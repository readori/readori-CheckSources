# booksource-auto-builder

Standalone helper for generating, repairing, and validating Readori/Legado-style
book source JSON.

The tool has two primary workflows:

- Single-site capture: inspect a reading website and generate a first-pass
  `book-source.json` package.
- Source-package repair: merge public Legado/Readori source packages, repair
  obvious metadata problems, deduplicate records, and emit a validation report.
- Offline scaffold/audit: create a skill-style work bundle before generation,
  and statically audit existing sources for placeholders, risky rules, search
  previews, and optional embedded JavaScript syntax errors.

It is intentionally conservative. Static validation proves that a source has the
minimum import/runtime fields, not that every target website is currently
readable. Runtime validation should still be done in Readori or with
`scripts/validate_source_packages.py`.

## 1. Install

```bash
cd tools/booksource-auto-builder
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Offline scaffold and audit modes do not require `requests` or `beautifulsoup4`.
Network capture and public-package repair do require the packages above.

## 2. Single-Site Capture

```bash
python capture.py \
  --base https://www.example.com \
  --name ExampleSource \
  --search-url /search?keyword=test \
  --detail-url /book/123 \
  --toc-url /book/123/catalog \
  --chapter-url /book/123/1 \
  --output output.source.json
```

Default outputs:

- `output.source.json`: importable JSON array containing one source.
- `output.source.json.report.json`: diagnostics, notes, and static issues.

Use `--output-dir` when you want a skill-style bundle:

```bash
python capture.py \
  --base https://www.example.com \
  --name ExampleSource \
  --detail-url /book/123 \
  --chapter-url /book/123/1 \
  --output-dir output/example-source
```

Bundle outputs:

- `book-source.json`: importable Readori/Legado source array.
- `assessment.md`: generation risk assessment.
- `analysis.md`: fetched stage coverage and inferred rules.
- `validation-checklist.md`: manual/runtime validation checklist.
- `report.json`: machine-readable diagnostics.

When only the website root is known, let the tool execute the inferred search
URL and follow the first likely detail, TOC, and chapter samples:

```bash
python capture.py \
  --base https://www.example.com \
  --name ExampleSource \
  --search-keyword test \
  --auto-follow-samples \
  --output-dir output/example-source
```

`--auto-follow-samples` is still heuristic, but it gives the generator real
search/detail/TOC/content evidence before writing rules. Use explicit
`--detail-url`, `--toc-url`, or `--chapter-url` when a site needs exact samples.

## 3. Offline Scaffold

Create the `book-source.json` / `assessment.md` / `analysis.md` /
`validation-checklist.md` bundle before fetching a site:

```bash
python capture.py \
  --scaffold-output-dir output/sites \
  --scaffold-site-url https://www.example.com
```

This is useful for the `book-source-creator-skill` workflow where assessment
must be written before generating final rules. The command does not overwrite
existing bundle files.

## 4. Static Audit

Audit one or more existing Legado/Readori source packages:

```bash
python capture.py \
  --audit-file ../../docs/Json/shareBookSource.json \
  --audit-output output/shareBookSource.audit.md \
  --report output/shareBookSource.audit.report.json \
  --audit-keyword 测试 \
  --audit-page 1
```

Audit checks include:

- Import-critical static issues from repair mode.
- Placeholder rule text left in source fields.
- Risky rule fields using JavaScript, regex replacement chains, WebView, or
  `java.*` helpers.
- Search URL preview after common `{{key}}` / `{{page}}` replacements.
- Optional embedded JavaScript syntax checks when Node.js is available.

This audit is intentionally weaker than runtime validation. It is designed to
catch bad packages before import and to tell operators where manual/runtime
verification is still required.

## 5. Batch Capture

Prepare a JSON array task file, for example `examples/batch-input.json`, then run:

```bash
python capture.py \
  --batch-file examples/batch-input.json \
  --batch-output-dir batch-output \
  --report batch-output/batch.report.json
```

Each task output is an importable one-source JSON array:

- `batch-output/*.source.json`
- `batch-output/batch.report.json`

The repository also includes real-site templates:

- `examples/batch-input.real.json`
- `examples/batch-command.real.txt`

## 6. Source-Package Repair

Repair a local package:

```bash
python capture.py \
  --repair-file ../../docs/Json/shareBookSource.json \
  --package-output output/repaired-sources.json \
  --report output/repaired-sources.report.json
```

Fetch and repair remote public package URLs:

```bash
python capture.py \
  --source-url https://legado.aoaostar.com/sources/71e56d4f.json \
  --source-url https://legado.aoaostar.com/sources/2a1f129b.json \
  --package-output output/public-sources.repaired.json \
  --report output/public-sources.report.json
```

Use the built-in public package URL list:

```bash
python capture.py \
  --public-sources \
  --package-output output/public-sources.repaired.json \
  --report output/public-sources.report.json
```

Discover public package URLs from public index pages before repair:

```bash
python capture.py \
  --discover-public-sources \
  --public-source-index-url https://legado.aoaostar.com/ \
  --discover-limit 80 \
  --usable-only \
  --package-output output/public-sources.discovered.usable.json \
  --report output/public-sources.discovered.usable.report.json
```

Discovery extracts JSON package links from `href`, `src`, common `data-*`
attributes, and embedded script strings, then merges them with the built-in
public package list before dedupe and static validation.

Only output records that pass static validation:

```bash
python capture.py \
  --public-sources \
  --usable-only \
  --package-output output/public-sources.usable.json \
  --report output/public-sources.usable.report.json
```

Repair mode supports array packages and common wrappers such as `sources`,
`bookSources`, `passedSources`, `list`, and `data`, including nested objects.

Static checks include:

- `bookSourceUrl` is an HTTP/HTTPS URL or can be recovered from embedded rules.
- `bookSourceName` exists.
- `searchUrl + ruleSearch.bookList` or `exploreUrl + ruleExplore.bookList` exists.
- `ruleBookInfo` exists.
- `ruleToc.chapterList`, `ruleToc.chapterName`, and `ruleToc.chapterUrl` exist.
- `ruleContent.content` exists.
- duplicate URLs keep the highest-scored, most complete record.

## 7. CLI Reference

- `--base`: required in single mode; website root URL.
- `--name`: optional source name; defaults to host.
- `--search-url`: optional sample search result page.
- `--detail-url`: optional sample book detail page.
- `--toc-url`: optional sample TOC page.
- `--chapter-url`: optional sample chapter content page.
- `--search-keyword`: sample keyword used by `--auto-follow-samples`; default `test`.
- `--auto-follow-samples`: execute inferred search and follow likely
  detail/TOC/chapter links for stronger generated rules.
- `--output`: single-mode output path; default `output.source.json`.
- `--output-dir`: single-mode bundle directory.
- `--report`: diagnostics report path.
- `--timeout`: request timeout seconds; default `20`.
- `--retries`: fetch retry count; default `2`.
- `--retry-backoff`: retry backoff base seconds; default `0.7`.
- `--batch-file`: batch task JSON array file.
- `--batch-output-dir`: batch output directory; default `batch-output`.
- `--repair-file`: local source JSON package to repair; repeatable.
- `--source-url`: remote source package URL to fetch and repair; repeatable.
- `--public-sources`: use the built-in public package URL list.
- `--discover-public-sources`: crawl public source index pages for JSON package
  links before repair.
- `--public-source-index-url`: public index page used by
  `--discover-public-sources`; repeatable.
- `--discover-limit`: maximum discovered package URLs; default `80`.
- `--package-output`: repaired package output path.
- `--usable-only`: repair mode only; output only static-valid records.
- `--scaffold-output-dir`: create an empty skill-style bundle without network.
- `--scaffold-site-url`: site URL for scaffold mode; defaults to `--base`.
- `--audit-file`: local source JSON package to statically audit; repeatable.
- `--audit-output`: markdown audit output path.
- `--audit-keyword`: keyword for search URL previews; default `测试`.
- `--audit-page`: page value for search URL previews; default `1`.

## 8. Batch Task Format

`--batch-file` must be a JSON array. Each object can contain:

- `base`: required website root URL.
- `name`: optional source name.
- `search_url`: optional sample search page.
- `detail_url`: optional sample detail page.
- `toc_url`: optional sample TOC page.
- `chapter_url`: optional sample chapter page.
- `search_keyword`: optional keyword for automatic sample following.
- `auto_follow_samples`: optional boolean to enable search/detail/TOC/chapter
  auto-follow for this task.
- `output`: optional custom output path.

## 9. Design Notes

- The generator is heuristic and produces a first draft, not guaranteed perfect
  rules for every website.
- Complex JS-rendered, anti-crawler, login-only, or signed API websites usually
  need manual rules or webView/JS support in the main engine.
- Generated import files do not include tool-only `notes` or `diagnostics`
  fields. Those are kept in reports so the JSON remains clean for Readori import.
- Search form detection preserves GET/POST methods and hidden fields.
- Auto-follow mode executes the inferred search URL and samples detail, TOC,
  and chapter pages when explicit sample URLs are absent.
- TOC detection handles detail pages that are also TOC pages and can follow
  common catalog links from detail pages.
- Public-package repair does not discard runtime-invalid websites unless
  `--usable-only` is requested; use the report to inspect failures.
- Public source discovery is bounded by `--discover-limit` and records every
  seed page in the machine-readable report.
- Scaffold/audit modes are offline-safe so they can run in CI or minimal Python
  environments before network dependencies are installed.
- Embedded JavaScript checks use Node.js when available and fall back to static
  risk reporting when Node.js is unavailable.

## 10. Runtime Validation

After static repair, run the project validator when network validation is needed:

```bash
python scripts/validate_source_packages.py \
  --input output/public-sources.usable.json \
  --report-path docs/Json/validation_report_runtime.json \
  --validated-output docs/Json/bookinfo_validated_sources.json \
  --validated-output-full docs/Json/bookinfo_validated_sources_full.json
```

The validator checks the four required runtime stages: search/explore, detail,
TOC, and chapter content. Those runtime results are stronger evidence than this
tool's static report.

## 11. Reference Alignment

The workflow is aligned with:

- Legado/Reading 3.0 rule model: `bookSourceUrl`, `bookSourceName`,
  `searchUrl` or `exploreUrl`, `ruleSearch`, `ruleBookInfo`, `ruleToc`, and
  `ruleContent`.
- `xy9144/auto_source_generator`: form detection, GET/POST search options,
  charset handling, TOC/content inference, chapter order checks, and partial
  output on failure.
- `Narylr350/book-source-creator-skill`: assess before generating, keep
  search/detail/TOC/content as explicit stages, prefer simple rules before JS,
  and treat static audit as weaker than runtime validation.
