"""Translate displayed tweet text to English with a local M2M100 model."""

from __future__ import annotations

import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

from . import db

MODEL = "gn64/M2M100_418M_CTranslate2"
REVISION = "18e406c615ef2991fa74d53734bf66b0a6b10cb4"
MODEL_FILES = ("config.json", "model.bin", "sentencepiece.bpe.model", "shared_vocabulary.json")
UNAVAILABLE = "[English translation unavailable]"
PRESERVED = re.compile(r"(https?://[^\s<>]+|[\r\n]+)")
HANDLE = re.compile(r"(?<!\w)@[A-Za-z0-9_]+")
INPUT_TOKENS = 384


def tweet_language(raw: str | None) -> str | None:
    """Read the source-language hint from X captures and compatible imports."""
    if not raw:
        return None
    try:
        post = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(post, dict):
        return None
    legacy = post.get("legacy")
    language = legacy.get("lang") if isinstance(legacy, dict) else None
    language = language or post.get("lang")
    if not isinstance(language, str):
        return None
    code = language.lower().split("-", 1)[0]
    code = {"in": "id", "iw": "he", "fil": "tl"}.get(code, code)
    return code if len(code) == 2 else None


def _snapshot(cache: Path, *, offline: bool) -> Path:
    from huggingface_hub import snapshot_download

    return Path(snapshot_download(
        MODEL, revision=REVISION, cache_dir=cache, allow_patterns=MODEL_FILES,
        local_files_only=offline, token=False,
    ))


def setup() -> Path:
    """Download the translation model for offline use."""
    return _snapshot(db.home() / "models", offline=False)


@lru_cache(maxsize=1)
def _detector():
    from lingua import LanguageDetectorBuilder

    return LanguageDetectorBuilder.from_all_languages().build()


@lru_cache(maxsize=1)
def _model(cache: Path):
    from ctranslate2 import Translator
    from sentencepiece import SentencePieceProcessor

    path = _snapshot(cache, offline=True)
    vocabulary = json.loads((path / "shared_vocabulary.json").read_text(encoding="utf-8"))
    languages = {token for token in vocabulary if re.fullmatch(r"__[a-z]{2,3}__", token)}
    tokenizer = SentencePieceProcessor(model_file=str(path / "sentencepiece.bpe.model"))
    return Translator(str(path), device="cpu", intra_threads=2), tokenizer, languages


@lru_cache(maxsize=512)
def _translate_text(text: str, language: str, cache: Path) -> str:
    translator, tokenizer, languages = _model(cache)
    source = f"__{language}__"
    if source not in languages:
        raise ValueError(f"Unsupported translation language {language}")
    pieces = tokenizer.encode(text, out_type=str)
    inputs = [
        [source, *pieces[i:i + INPUT_TOKENS], "</s>"]
        for i in range(0, len(pieces), INPUT_TOKENS)
    ]
    results = translator.translate_batch(
        inputs, target_prefix=[["__en__"]] * len(inputs), beam_size=2,
        max_decoding_length=2 * INPUT_TOKENS,
    )
    return " ".join(tokenizer.decode(result.hypotheses[0][1:]).strip() for result in results).strip()


def _latin_only(text: str) -> bool:
    return all(
        not char.isalpha() or unicodedata.name(char, "").startswith("LATIN")
        for char in text
    )


def _language(sample: str) -> str | None:
    values = _detector().compute_language_confidence_values(sample)
    if not values or not values[0].value:
        return None
    code = values[0].language.iso_code_639_1.name.lower()
    # A single Latin-script word is often an English name or identifier.
    minimum = 0.25 if len(sample.split()) == 1 else 0.1
    if code == "en" or values[0].value >= minimum or not _latin_only(sample):
        return code
    return None


def _english_part(part: str, hint: str | None) -> str:
    sample = HANDLE.sub("", part).strip()
    if not any(char.isalpha() for char in sample):
        return part
    latin = _latin_only(sample)
    code = _language(sample) if not hint or (hint == "en" and not latin) else hint
    if code is None:
        return part if latin else UNAVAILABLE
    if code == "en":
        return part

    text = part.strip()
    try:
        translated = _translate_text(text, code, db.home() / "models")
    except (ImportError, OSError, RuntimeError, ValueError):
        translated = ""
    if not translated or translated == text:
        # A model sometimes copies names or products verbatim.
        if text.isascii() and len(text.split()) == 1 and translated:
            return part
        return UNAVAILABLE
    start = len(part) - len(part.lstrip())
    end = len(part.rstrip())
    return part[:start] + translated + part[end:]


def english(text: str | None, source_language: str | None = None) -> str | None:
    if not text:
        return text
    parts = PRESERVED.split(text)
    if not source_language and len(parts) > 1:
        source_language = _language(HANDLE.sub("", " ".join(parts[::2])))
    return "".join(_english_part(part, source_language) if i % 2 == 0 else part for i, part in enumerate(parts))
