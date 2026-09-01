from validator import validate_source_packages as validator


def _source(url: str, name: str = "Example", *, search: str = "/search?q={{key}}") -> dict:
    return {
        "bookSourceName": name,
        "bookSourceUrl": url,
        "searchUrl": search,
        "ruleSearch": {"bookList": "//book", "name": "//title"},
        "ruleBookInfo": {"name": "//h1"},
    }


def test_url_normalization_and_rule_fingerprint_are_separate_layers() -> None:
    first = _source("HTTPS://Example.com:443/path/?b=2&a=1")
    same_rules = _source("https://example.com/path?a=1&b=2", "Renamed")
    different_rules = _source("https://example.com/path?a=1&b=2", search="/find?q={{key}}")

    assert validator.canonical_source_site_key(first) == validator.canonical_source_site_key(same_rules)
    assert validator.source_rule_fingerprint(first) == validator.source_rule_fingerprint(same_rules)
    assert validator.source_rule_fingerprint(first) != validator.source_rule_fingerprint(different_rules)
    assert validator.source_dedupe_key(first) == validator.source_dedupe_key(same_rules)
    assert validator.source_dedupe_key(first) != validator.source_dedupe_key(different_rules)


def test_group_sources_collapses_cosmetic_variants_but_keeps_rule_variants() -> None:
    groups = validator.group_sources(
        [
            _source("https://example.com/path/"),
            _source("https://EXAMPLE.com:443/path#export-label", "Renamed"),
            _source("https://example.com/path", search="/find?q={{key}}"),
        ]
    )
    assert len(groups) == 2
    assert sorted(len(items) for items in groups.values()) == [1, 2]


def test_book_author_aggregation_is_scoped_to_one_site() -> None:
    first = _source("https://example.com", "A")
    first["__readoriValidation"] = {
        "bookTitle": "The  Book!",
        "bookAuthor": "作者：Jane Doe",
        "sampleBookUrl": "https://example.com/book/1",
    }
    same_book_variant = _source("https://example.com/#variant", "B")
    same_book_variant["__readoriValidation"] = {
        "bookTitle": "the book",
        "bookAuthor": "Jane   Doe",
        "sampleBookUrl": "https://example.com/book/1",
    }
    other_site = _source("https://other.example", "Other")
    other_site["__readoriValidation"] = {
        "bookTitle": "The Book",
        "bookAuthor": "Jane Doe",
        "sampleBookUrl": "https://other.example/book/1",
    }

    output, stats = validator.aggregate_validated_sources(
        [("a", first, None), ("b", same_book_variant, None), ("c", other_site, None)]
    )
    assert len(output) == 2
    assert stats == {"input": 3, "output": 2, "removed": 1, "duplicateGroups": 1}
    assert {item["bookSourceUrl"] for item in output} == {"https://example.com", "https://other.example"}
