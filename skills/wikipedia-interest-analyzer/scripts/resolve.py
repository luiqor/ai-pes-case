"""Resolve a natural-language topic into concrete Wikipedia articles.

Strategy (verified live against Wikidata Q1666254 / Q44602):

1. Search Wikidata for the topic -> a Q-item ("intermittent fasting" -> Q1666254).
2. Read that item's sitelinks for each requested language edition.
3. **Confirm** every title with ``prop=info`` -- sitelinks and search results can
   be stale (observed: a deleted article still ranked in search results).
4. For any language with no sitelink (a genuine coverage gap), offer the nearest
   *candidates* from that wiki's own search index -- as candidates only. This
   script never substitutes a proxy article for a missing one; it reports the
   gap and lets the user decide.

Usage:
    resolve.py --topic "intermittent fasting" --langs pl,cs [--out resolution.json]
    resolve.py --qid Q1666254 --langs pl,cs            # skip the search step
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import common


def _search_concept(topic: str, language: str, limit: int) -> dict[str, Any]:
    """Wikidata entity search. Returns the raw payload so callers can report
    the full candidate list rather than silently taking the first hit."""
    return common.wikidata_api(
        {
            "action": "wbsearchentities",
            "search": topic,
            "language": language,
            "type": "item",
            "limit": str(limit),
        }
    )


def _entity(qid: str, languages: list[str]) -> dict[str, Any]:
    wanted = sorted({f"{lang}wiki" for lang in languages})
    return common.wikidata_api(
        {
            "action": "wbgetentities",
            "ids": qid,
            "props": "labels|descriptions|sitelinks",
            "languages": "|".join(sorted(languages)),
            "sitefilter": "|".join(wanted),
        }
    )


def _confirm_titles(language: str, titles: list[str]) -> dict[str, Any]:
    """Batch ``prop=info`` lookup. Returns the raw query payload."""
    if not titles:
        return {}
    return common.action_api(
        common.project_for(language),
        {"action": "query", "titles": "|".join(titles), "prop": "info"},
    )


def _search_wiki(language: str, term: str, limit: int) -> dict[str, Any]:
    return common.action_api(
        common.project_for(language),
        {
            "action": "query",
            "list": "search",
            "srsearch": term,
            "srnamespace": "0",
            "srlimit": str(limit),
        },
    )


def _confirmed_candidates(
    language: str, term: str, limit: int, warnings: list[str]
) -> list[dict[str, Any]]:
    """Search the wiki, then drop any hit that no longer exists.

    The stale-index case was observed directly: an article ranked in search
    results while ``prop=info`` reported it missing.
    """
    if not term:
        return []
    hits = _search_wiki(language, term, limit * 3).get("query", {}).get("search", [])
    if not hits:
        return []

    titles = [hit.get("title", "") for hit in hits if hit.get("title")]
    info = _confirm_titles(language, titles)

    out: list[dict[str, Any]] = []
    for hit in hits:
        title = hit.get("title", "")
        exists, pageid = common.page_exists(info, title)
        if not exists:
            warnings.append(
                f"{language}: search result {title!r} no longer exists "
                "(stale search index) -- dropped"
            )
            continue
        out.append(
            {
                "title": title,
                "pageid": pageid,
                "snippet": common.strip_html(hit.get("snippet", "")),
            }
        )
        if len(out) >= limit:
            break
    return out


def resolve(
    topic: str,
    languages: list[str],
    *,
    qid: str | None = None,
    search_language: str = "en",
    candidate_limit: int = 3,
    search_limit: int = 5,
) -> dict[str, Any]:
    """Resolve ``topic`` into one article per language edition.

    Returns a dict containing ``articles`` (title-or-null per language),
    ``gaps`` (languages with no article) and ``candidates`` (nearest known
    articles for those gaps -- for the user to choose from, never auto-picked).
    """
    warnings: list[str] = []

    # --- 1. concept -> Q-item -------------------------------------------------
    search_hits: list[dict[str, Any]] = []
    if qid is None:
        payload = _search_concept(topic, search_language, search_limit)
        search_hits = [
            {
                "id": hit.get("id"),
                "label": hit.get("label"),
                "description": hit.get("description"),
            }
            for hit in payload.get("search", [])
        ]
        if not search_hits:
            raise common.ApiError(
                f"Wikidata has no entity matching {topic!r} in "
                f"language {search_language!r}; cannot resolve a concept.",
                kind="not_found",
            )
        qid = search_hits[0]["id"]

    # --- 2. Q-item -> labels + sitelinks -------------------------------------
    label_langs = sorted(set(languages) | {search_language})
    entity_payload = _entity(qid, label_langs)
    entity = entity_payload.get("entities", {}).get(qid) or {}
    if not entity or entity.get("missing") is not None:
        raise common.ApiError(
            f"Wikidata entity {qid} does not exist", kind="not_found"
        )

    labels = {
        lang: data.get("value")
        for lang, data in (entity.get("labels") or {}).items()
        if data.get("value")
    }
    descriptions = {
        lang: data.get("value")
        for lang, data in (entity.get("descriptions") or {}).items()
        if data.get("value")
    }
    sitelinks = entity.get("sitelinks") or {}

    # --- 3. sitelinks -> titles (confirmed) ----------------------------------
    titles_by_lang: dict[str, str] = {}
    for language in languages:
        raw = (sitelinks.get(f"{language}wiki") or {}).get("title")
        if raw:
            titles_by_lang[language] = raw

    articles: dict[str, dict[str, Any] | None] = {}
    gaps: list[str] = []
    for language in languages:
        title = titles_by_lang.get(language)
        if not title:
            articles[language] = None
            gaps.append(language)
            continue
        info = _confirm_titles(language, [title])
        exists, pageid = common.page_exists(info, title)
        if not exists:
            warnings.append(
                f"{language}: sitelink {title!r} points at a page that does not "
                "exist (stale sitelink) -- treated as a coverage gap"
            )
            articles[language] = None
            gaps.append(language)
        else:
            articles[language] = {"title": title, "pageid": pageid, "source": "sitelink"}

    # --- 4. gaps -> nearest candidates (reported, never substituted) ----------
    candidates: dict[str, list[dict[str, Any]]] = {}
    for language in gaps:
        term = labels.get(language) or labels.get(search_language) or topic
        candidates[language] = _confirmed_candidates(
            language, term, candidate_limit, warnings
        )

    return {
        "topic": topic,
        "search_language": search_language,
        "qid": qid,
        "label": labels.get(search_language) or topic,
        "description": descriptions.get(search_language),
        "labels": labels,
        "languages": list(languages),
        "articles": articles,
        "gaps": gaps,
        "candidates": candidates,
        "search_hits": search_hits,
        "warnings": warnings,
    }


def parse_languages(raw: str) -> list[str]:
    languages = [part.strip().lower() for part in raw.split(",") if part.strip()]
    if not languages:
        raise argparse.ArgumentTypeError("at least one language code is required")
    for language in languages:
        try:
            common.project_for(language)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(str(exc)) from exc
    return languages


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--topic", help="natural-language topic, e.g. 'intermittent fasting'")
    parser.add_argument("--langs", type=parse_languages, required=True,
                        help="comma-separated language codes, e.g. pl,cs,uk")
    parser.add_argument("--qid", help="skip topic search and use this Wikidata id (e.g. Q1666254)")
    parser.add_argument("--search-lang", default="en",
                        help="language for the Wikidata concept search (default: en)")
    parser.add_argument("--candidates", type=int, default=3,
                        help="candidate articles to offer per gap language (default: 3)")
    parser.add_argument("--limit", type=int, default=5,
                        help="Wikidata search hits to report (default: 5)")
    parser.add_argument("--out", help="write JSON here instead of stdout")
    parser.add_argument("--no-cache", action="store_true", help="ignore cached responses")
    return parser


def main(argv: list[str] | None = None) -> int:
    common.configure_console()
    args = build_parser().parse_args(argv)
    if not args.topic and not args.qid:
        print("error: provide --topic or --qid", file=sys.stderr)
        return 2
    if args.no_cache:
        common.set_cache_enabled(False)

    try:
        result = resolve(
            args.topic or args.qid,
            args.langs,
            qid=args.qid,
            search_language=args.search_lang,
            candidate_limit=args.candidates,
            search_limit=args.limit,
        )
    except common.ApiError as exc:
        print(f"error [{exc.kind}]: {exc}", file=sys.stderr)
        return 1

    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
