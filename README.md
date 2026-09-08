# dsh-autotrans

A **DeepSeek Harness (DSH) plugin bundle** that translates English academic PDFs and
books into Chinese via the DeepSeek API, reusing the proven AutoTrans pipeline:

```
PDF → extract (structured text) → translate (DeepSeek, glossary + resume) → DOCX / EPUB
```

It installs as a normal DSH plugin and registers one model-facing tool, `autotrans`,
so the agent can translate a document directly. The heavy lifting stays in the bundled
Python core; the Node plugin is a thin, zero-dependency process wrapper.

[中文说明](README.zh.md)

## Features

- **Extract** — layout-aware PDF extraction (single/two-column detection, heading
  detection, header/footer/figure-caption/reference filtering) → `extracted.json` + a
  plain-text `preview/`.
- **Translate** — batch DeepSeek translation with terminology-glossary constraints,
  per-paragraph resumable checkpointing (re-running only fills missing segments, so it
  never re-bills finished work), and configurable concurrency.
- **Render** — Chinese `DOCX` (generated with the standard library, no `python-docx`)
  and/or `EPUB` (via `ebooklib`).
- **Glossaries** — built-in `general`, `biomedical`, `anthropology` term tables, or your
  own TSV (`English<TAB>Chinese` per line).

## Requirements

- A running DSH with `pnpm` on `PATH` (for `dsh plugin`).
- Python **3.10+** with the pipeline dependencies available to whichever interpreter the
  plugin invokes (default `python`):

  ```sh
  pip install pymupdf ebooklib
  ```

  `pymupdf` is required for extraction; `ebooklib` is only required for EPUB output
  (DOCX needs no third-party package).

## Install into DSH

Push this repository to GitHub, then on each device run:

```sh
dsh plugin --profile web add github.com/<your-name>/dsh-autotrans
# then restart the profile (stop and start `dsh web`) so the tool registers
```

This works for any profile, not just `web`:

```sh
dsh plugin --profile my-profile add github.com/<your-name>/dsh-autotrans
```

`dsh plugin` forwards to pnpm inside the profile directory, then reconciles
`dsh.profile.bundles`: because this package declares `dsh.bundle.patch`, it is appended
as a profile layer automatically. There is no build step, so no `allowBuilds` entry is
needed.

> Local checkout instead of GitHub:
> `dsh plugin --profile web add file:<path-to-this-repo>` (or `link:<path>`).

## Usage (from DSH)

Once the profile restarts, the agent has an `autotrans` tool. Ask it to translate a
PDF, for example:

> 把 `D:\papers\paper.pdf` 翻译成中文 DOCX

The tool accepts:

| Argument | Description |
|---|---|
| `pdf_path` | Absolute path to a text-based PDF (required). |
| `action` | `all` (default) / `extract` / `translate` / `render`. |
| `out_dir` | Output directory (default `<pdf>_translated/`). |
| `glossary` | `general` / `biomedical` / `anthropology`, or a TSV path. |
| `model` | DeepSeek model (default `deepseek-chat`). |
| `concurrency` | Concurrent requests (default 4). |
| `format` | `docx` / `epub` / `both`. |
| `api_key` | DeepSeek API key (otherwise `DEEPSEEK_API_KEY` env var). |
| `config_path` | A JSON config file for advanced options. |

Set the API key once in the environment where DSH runs (`DEEPSEEK_API_KEY=sk-...`), or
pass it per call via `api_key`.

## Standalone CLI

The bundled Python entry can also be used directly:

```sh
python python/autotrans.py all D:/papers/paper.pdf --glossary biomedical
python python/autotrans.py extract  paper.pdf   # inspect extraction quality first
python python/autotrans.py translate paper.pdf  # needs DEEPSEEK_API_KEY
python python/autotrans.py render   paper.pdf
```

Outputs land in `<pdf>_translated/`: `extracted.json`, `preview/article.txt`,
`translated_final.json`, and the rendered `.docx`/`.epub`.

## Configuration

The bundle's `cordis.patch.yml` inserts one row with sensible defaults. Override them in
the profile's own `cordis.patch.yml` (see
[Profiles](https://github.com/deepseek-ai/deepseek-harness) in the DSH docs):

```yaml
- id: tool-autotrans
  config:
    python: python3            # interpreter that has pymupdf + ebooklib
    model: deepseek-chat
    concurrency: 4
    format: docx
```

Or disable the tool:

```yaml
- id: tool-autotrans
  disabled: true
```

Advanced extraction/translation thresholds live in a JSON config file
(`python/config.example.json`): extract geometry, batch sizes, temperature, custom system
prompts, and output file names.

## Project layout

```
dsh-autotrans/
├─ package.json            # dsh.bundle.patch → cordis.patch.yml (zero runtime deps)
├─ cordis.patch.yml        # inserts the tool-autotrans row
├─ lib/
│  └─ index.js             # cordis plugin: registers the `autotrans` tool
├─ python/
│  ├─ autotrans.py         # CLI entry (extract/translate/render/all + JSON summary)
│  ├─ core/                # config / extract / translate / render
│  ├─ glossaries/          # general / biomedical / anthropology TSV
│  └─ config.example.json
├─ requirements.txt
├─ README.md / README.zh.md
└─ LICENSE
```

## Notes

- **Scanned PDFs**: extraction expects a text layer; run OCR first (not bundled).
- **Cost**: `deepseek-chat` usually costs only a few RMB for an article.
- **Resume**: each segment is written as soon as it is translated; re-running only fills
  missing segments.

## License

[MIT](LICENSE)
