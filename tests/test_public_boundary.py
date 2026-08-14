"""Mechanical guard for the private-to-public extraction boundary.

Sensitive source terms are represented only by SHA-256 digests of normalized
phrases. The test scans both paths and text, so a private identifier cannot be
hidden in a filename. Large/generated evidence shapes are rejected separately.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MAX_NGRAM = 12
MAX_SOURCE_BYTES = 2 * 1024 * 1024
MAX_FIXTURE_BYTES = 512 * 1024

ALLOWED_TOP_LEVEL = frozenset(
    {
        ".claude-plugin",
        ".codex-plugin",
        ".env.example",
        ".gitattributes",
        ".github",
        ".gitignore",
        "AGENTS.md",
        "CLAUDE.md",
        "CONTRIBUTING.md",
        "LICENSE",
        "MANIFEST.in",
        "README.md",
        "SECURITY.md",
        "docs",
        "pyproject.toml",
        "skills",
        "src",
        "tests",
    }
)

GENERATED_SDIST_TOP_LEVEL = frozenset({"PKG-INFO", "setup.cfg"})

SKIPPED_DIRECTORIES = frozenset(
    {
        ".git",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "build",
        "dist",
    }
)

FORBIDDEN_DIRECTORY_NAMES = frozenset(
    {
        ".benchmarks",
        "compare_results",
        "extracted",
        "input",
        "llm-wikis",
        "logs",
        "migration",
        "research",
    }
)

TEXT_SUFFIXES = frozenset(
    {
        "",
        ".cfg",
        ".css",
        ".example",
        ".html",
        ".in",
        ".ini",
        ".js",
        ".json",
        ".md",
        ".py",
        ".rst",
        ".toml",
        ".txt",
        ".yaml",
        ".yml",
    }
)

FORBIDDEN_ARTIFACT_SUFFIXES = frozenset(
    {
        ".7z",
        ".db",
        ".gz",
        ".jsonl",
        ".pdf",
        ".sqlite",
        ".sqlite3",
        ".tar",
        ".zip",
    }
)

# Generated privately from the approved denylist. Generic English phrases are
# excluded even if they also happen to be a profile slug; this guard is for
# sensitive or distinctive material, not accidental title-word overlap.
BANNED_HASHES = frozenset(
    {
        "077cf4c3927fd037d9600845a77bc97d380df318d446e4100a6bec85c8c4b914",
        "07d761e70a4bd88edf561452536d0ca50eac5647ebeb1bf05784f9bb27f79485",
        "0d95a29931bff981b2756da9575039639bf161880123c79641d6bfd98792e9a1",
        "0e4e44a023ba49b639eec58f4ca47d299e56433a42c0ae40f099781c3ab50bee",
        "0e775558640a398d37e35f6cb2ca01f5fc9d39db1a98d2b6c6b66ce7d61a9958",
        "1eff97bcdab796f8603dc711ee54ddc58ea915243fbfe43978396cc4474ba31e",
        "1fb8d97aac461ed8f746cb5558ae6b48cb6474532d7bd9b87e33cd1720dcc393",
        "2100f0f3e77e8553ad9f06cfa12837f040912366f1450683fdaa3c028ff8f741",
        "2f6c219ece22f6216ac7b8ce9efbfebc5d0b53430787ed029f5c00fd954a1314",
        "4639af4669a0fab033acd1aef41a349bf00d02a60d385c47d6754d118b3b3344",
        "492c2ae9888a5b3bc2de08177f1c57051b7b0043b5d8a417592e9fdc4d76c968",
        "49352e4082ea9db6224dadcf3471b333d1745e7b71079e209e2e6d4869b8acc7",
        "5424072da5a0bef8c5706ba31943a8a2c5eab58501361983fe8095d7a9678746",
        "6d0696a8736b04ef36975c006544af61478aae8e9b9da03bbf123ddcce175952",
        "6e2fa082db28a2fb1d28fa3f4562867c410ccdb86632b9b91c56879302783b3e",
        "70b27deb2ba84ace5eb4d26d1ee72971f59b69d86a0686f70fb58b894dace245",
        "764b8c1fcab9608336505152aea2519c728ffbca842bc808d0302c5ca9f09dc7",
        "76b4eb4bf1925d1623874349bc20ad102aa6ae65481dd7e97528abacdaa6ccc6",
        "7a171c9f7488729c4c28a8005e5e95126b7a0531d2937b06b41b9e8de7de0497",
        "88a72030efe002e656aaae29d2b1cd09f326d358625fdce80145408727a754c6",
        "940e51f19957130efc503b5d68b679c658f13dbf69c341d5cc0b676f9eb808a2",
        "965242bd26fb5f532489056ccb4fd37612bb0c27ee5e25d918a5d4b300d8e7eb",
        "9e575246238a00029d2ab05654e8f15908308f8c2e506e96d38a3e7a734e5f3f",
        "a136312c6806160c31ef0b5558456e81dd6054e52c4c23d5cf8be6838d469d59",
        "a16a0885aa47d5eec9aeb632daefa5f10add17521daea2a1e5900c5df0ef775b",
        "a3539d7ec73462c618fe12ae549863b95b449aa396124d19cc7c969250983bab",
        "a63e526ecbaa660a38cc2694713410458529dd6c6eba5985f4d3754a5dba3217",
        "a95f005d3ca1b6341da53a39f030959eab70356c92ca640d1ff2ab572b3b8016",
        "adfb13b78b2683fed5aef9d5150215de95f1f036862070eb2d159740d2a8f96d",
        "b12f6f228c8b79abd1e91b4d1f00cae85b9b3f675b477641a4f8a5d7c73cde39",
        "b6b9aad3e7ae336f7e5cbd1515fc7d1d8f29cf7c257012dfb474be3b44d9fa2c",
        "b6dadc4feed52c37c8b3070f5c4c1d56b0debfee32f39b7ea7912a8c9af677c2",
        "bacd1f7494aeaf4714b66651cf39f0289eca4ae741d7b6c8619f8ad29b7ea701",
        "bb99758d9f4dec9ecf3dc2651da1a2ccc1c7d311d37bf9ea06933886ef891691",
        "bc51f804b0cdd92fbfb388e92b461cf4484d220840de0e4764efc6967ea34310",
        "c87f297cbf429cc6142245a8ef0b82a436fc078b4329a4ab66994213b417b12a",
        "cfa08d1c93e7e7f4e4a2f541044c755e23f294e9e053cbda063c5b23854ee38c",
        "d14468d2b1a0e134fa24ead8169852f5c7dfe09a01dc6b734991522de04a4a61",
        "d197ecf9079cde0df221f7bbd945b54f5ef510ec62b291ac2d3c9add3a112d16",
        "d1f9181966b0e788e78a69d46adcda3e7d40adecda22f1c90f11bfd5f8ec70c1",
        "dc440aa4559cc0ffa3ed2a674ab8f66451ca8581ce828a1b8b4723c9bab963ee",
        "de191c787d2c054ce9dc35ec42c9a436ee89e400c6e6345c0095f86b237c951c",
        "e3cda65904433362d299bc2d23c74277b348a87e27773801ea8c295e737dc03d",
        "e582fce3fe3915aa308d4f5ccca58e11e16a9a0a4ae7bc8be984d62344959514",
        "e89f09c2621edf7f81c7b476be8645ef46d9aed19d8238bca65a4052dcf65788",
        "e9a6c4ba1e2c8fb8185c2db4f5d7145fbf580b83d98670e49c732d82f90ba3d8",
        "ecf4d632ebb73b6b593b2fb2db4395c76906f35ba32d6875da5da3ccc7ed3eca",
        "ed9478bf2157ec2f64a0eff420804713ae4183800a9698bb7edf2153471034e1",
        "f066e8c9daad863e6ff0a0f232a152194bb5768c6d9da76a08add70e874e5055",
        "f70dec0be73b9669714ad3defeee3832fcef6faed80322191ac28bed26f3007d",
        "fd1866a7637abe7548b350b5d14d0ab713759406953c474a290d996a6917a505",
    }
)

WINDOWS_MACHINE_PATH = re.compile(r"(?i)(?<![a-z0-9])[a-z]:\\(?:users|projects)\\[^\s\"']+")
POSIX_HOME_PATH = re.compile(r"(?<![a-z0-9])/(?:home|users)/[^/\s]+/")
EMAIL_ADDRESS = re.compile(r"(?i)\b[a-z0-9._%+-]+@(?:[a-z0-9-]+\.)+[a-z]{2,}\b")
PRIVATE_IPV4 = re.compile(
    r"(?<!\d)(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|"
    r"192\.168\.\d{1,3}\.\d{1,3}|"
    r"172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})(?!\d)"
)
SECRET_SHAPE = re.compile(r"(?i)\b(?:sk|key|token|secret)[-_][a-z0-9]{16,}\b")
SAFE_EMAIL_DOMAINS = (".example", ".invalid", "example.com", "example.org", "example.net")


def normalized_tokens(value: str) -> list[str]:
    decomposed = unicodedata.normalize("NFKD", value)
    without_marks = "".join(character for character in decomposed if not unicodedata.combining(character))
    return re.sub(r"[^a-z0-9]+", " ", without_marks.lower()).split()


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def ngram_hashes(value: str) -> set[str]:
    tokens = normalized_tokens(value)
    hashes: set[str] = set()
    for size in range(1, min(MAX_NGRAM, len(tokens)) + 1):
        for index in range(len(tokens) - size + 1):
            hashes.add(digest(" ".join(tokens[index : index + size])))
    return hashes


def repository_files() -> list[Path]:
    files: list[Path] = []
    for path in REPOSITORY_ROOT.rglob("*"):
        relative = path.relative_to(REPOSITORY_ROOT)
        if SKIPPED_DIRECTORIES.intersection(relative.parts):
            continue
        if path.is_file():
            files.append(path)
    return sorted(files)


def is_fixture(path: Path) -> bool:
    relative = path.relative_to(REPOSITORY_ROOT)
    return len(relative.parts) >= 2 and relative.parts[:2] == ("tests", "fixtures")


def is_extracted_sdist_tree() -> bool:
    """Recognize only the two standard metadata files added by ``sdist``."""
    return not (REPOSITORY_ROOT / ".git").exists() and all(
        (REPOSITORY_ROOT / name).is_file() for name in GENERATED_SDIST_TOP_LEVEL
    )


class PublicBoundaryTests(unittest.TestCase):
    def test_only_approved_top_level_entries_exist(self) -> None:
        sdist_tree = is_extracted_sdist_tree()
        unexpected = sorted(
            path.name
            for path in REPOSITORY_ROOT.iterdir()
            if path.name != ".git"
            and path.name not in SKIPPED_DIRECTORIES
            and path.name not in ALLOWED_TOP_LEVEL
            and not (sdist_tree and path.name in GENERATED_SDIST_TOP_LEVEL)
        )
        self.assertEqual(unexpected, [])

    def test_no_forbidden_private_directory_shape_exists(self) -> None:
        offenders: list[str] = []
        for path in REPOSITORY_ROOT.rglob("*"):
            relative = path.relative_to(REPOSITORY_ROOT)
            if SKIPPED_DIRECTORIES.intersection(relative.parts):
                continue
            if FORBIDDEN_DIRECTORY_NAMES.intersection(relative.parts):
                offenders.append(relative.as_posix())
        self.assertEqual(offenders, [])

    def test_no_private_term_occurs_in_a_path_or_text_file(self) -> None:
        offenders: list[str] = []
        for path in repository_files():
            relative = path.relative_to(REPOSITORY_ROOT).as_posix()
            path_hits = ngram_hashes(relative).intersection(BANNED_HASHES)
            if path_hits:
                offenders.append(f"{relative}: private path hash")
                continue
            if path.suffix.lower() not in TEXT_SUFFIXES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if ngram_hashes(text).intersection(BANNED_HASHES):
                offenders.append(f"{relative}: private text hash")
        self.assertEqual(offenders, [])

    def test_no_machine_path_email_endpoint_or_secret_shape(self) -> None:
        offenders: list[str] = []
        for path in repository_files():
            if path.suffix.lower() not in TEXT_SUFFIXES:
                continue
            relative = path.relative_to(REPOSITORY_ROOT).as_posix()
            text = path.read_text(encoding="utf-8", errors="replace")
            if WINDOWS_MACHINE_PATH.search(text) or POSIX_HOME_PATH.search(text):
                offenders.append(f"{relative}: absolute machine path")
            if PRIVATE_IPV4.search(text):
                offenders.append(f"{relative}: private network endpoint")
            if SECRET_SHAPE.search(text):
                offenders.append(f"{relative}: credential-shaped value")
            for match in EMAIL_ADDRESS.finditer(text):
                domain = match.group(0).rsplit("@", 1)[1].lower()
                if not domain.endswith(SAFE_EMAIL_DOMAINS):
                    offenders.append(f"{relative}: non-placeholder email address")
                    break
        self.assertEqual(offenders, [])

    def test_generated_or_archival_artifacts_are_not_admitted(self) -> None:
        offenders: list[str] = []
        for path in repository_files():
            suffix = path.suffix.lower()
            relative = path.relative_to(REPOSITORY_ROOT).as_posix()
            if suffix in FORBIDDEN_ARTIFACT_SUFFIXES and not is_fixture(path):
                offenders.append(relative)
            if is_fixture(path) and path.stat().st_size > MAX_FIXTURE_BYTES:
                offenders.append(f"{relative}: fixture exceeds size cap")
            elif not is_fixture(path) and path.stat().st_size > MAX_SOURCE_BYTES:
                offenders.append(f"{relative}: source file exceeds size cap")
        self.assertEqual(offenders, [])

    def test_hash_guard_is_live(self) -> None:
        self.assertGreaterEqual(len(BANNED_HASHES), 50)
        sample_hash = digest("ordinary synthetic phrase")
        self.assertIn(sample_hash, ngram_hashes("an ordinary synthetic phrase here"))
        self.assertTrue({sample_hash}.intersection({sample_hash}))

    def test_generic_reasoning_phrase_is_not_private(self) -> None:
        phrase_hash = digest("build a reasoning model")
        self.assertNotIn(phrase_hash, BANNED_HASHES)


if __name__ == "__main__":
    unittest.main()
