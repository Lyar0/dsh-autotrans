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

The interpreter the plugin runs can be one of three things (see "Self-contained
runtime" below, or the plain-Python section):

1. **A prebuilt self-contained runtime** — recommended. No Python install needed on the
   device.
2. **A system Python** (Python **3.10+**) with the pipeline dependencies:

   ```sh
   pip install pymupdf ebooklib
   ```

   `pymupdf` is required for extraction; `ebooklib` is only required for EPUB output
   (DOCX needs no third-party package).

## Self-contained runtime (no system Python needed)

`scripts/build-runtime.ps1` packages the AutoTrans Python core **and its dependencies**
into a Windows x64 runtime that runs without any system Python installed. The plugin's
`config.python` default is `auto`, which automatically uses a runtime placed at
`<plugin>/runtime/autotrans.exe`.

**Build once (on Windows, in a Python that has `pymupdf`, `ebooklib`, `pyinstaller`):**

```powershell
powershell -ExecutionPolicy Bypass -File scripts/build-runtime.ps1 -Python D:/Projects/CondaEnvs/booktrans/python.exe
# produces dist/autotrans/  and  dist/autotrans-win-x64.zip (~30 MB)
```

**Publish** the resulting `dist/autotrans-win-x64.zip` as a GitHub Release asset of this
repo (tag it, then upload the zip). New devices fetch it once:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/get-runtime.ps1          # owner/repo default to this repo
# downloads autotrans-win-x64.zip from the latest release and drops autotrans.exe
# into <plugin>/runtime/, which the plugin's "auto" python mode detects
```

A single `autotrans.exe` invocation needs no config edit after that:

```
dist/autotrans/autotrans.exe --help
```

You can also point `config.python` directly at the exe path (or at a directory that
contains `autotrans.exe`) to bypass auto-detection.

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

Outputs are written under the caller workspace when the DSH tool is used, or
alongside the PDF for a standalone run, into `<output>/autotrans/<pdf-stem>/`:
`extracted.json`, `preview/article.txt`, `translated_final.json` and the rendered
`.docx`/`.epub`. Explicitly pass `--out-dir` to override.

## Configuration

The bundle's `cordis.patch.yml` inserts one row with sensible defaults. Override them in
the profile's own `cordis.patch.yml` (see
[Profiles](https://github.com/deepseek-ai/deepseek-harness) in the DSH docs):

```yaml
- id: tool-autotrans
  config:
    python: auto              # auto = packaged runtime, else system python; or an exe path
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

`translation.auto_glossary` is on by default: before translating, the model scans the
document and builds a `auto_glossary.tsv` domain glossary for the current literature,
then merges it with the one selected via `--glossary` (manual entries win on conflicts).
This is the mechanism that adapts to a brand-new research field without you hand-writing
a glossary; the generated file is cached next to the output and can be deleted to
regenerate.

**Local term library that grows with use.** Validated/auto word pairs also accumulate
into a persistent per-user glossary at `$DSH_HOME/autotrans/user_glossary.tsv`
(`DSH_HOME` defaults to `~/.dsh` for standalone runs). Every new/auto book term is
appended only once and entries there are merged into **all subsequent** translations;
an entry already present is never overwritten by a later auto term. The result: the
longer you use the tool, the fuller and more consistent your local terminology
becomes, including across research fields. You can also hand-edit this TSV
(`English<TAB>Chinese`) — your edits are authoritative and outrank auto terms. This
file lives outside the plugin's own folder, so a plugin reinstall does not erase it.

### Behavior notes on the pipeline

- **No mid-sentence splits**: long paragraphs are chunked **only at sentence
  boundaries** (never cut inside a sentence/word), so a single sentence is translated
  whole even if it exceeds the chunk size.
- **Hierarchical headings**: extraction records a heading `level` (prose/number heuristic:
  `1 Introduction`, `2.1 …`, `…`); DOCX render uses size-scaled, bold per level so chapter
  vs section vs subsection are visually distinct. EPUB renders headings as `h2`/`h3`.

## Project layout

```
dsh-autotrans/
├─ package.json            # dsh.bundle.patch → cordis.patch.yml (zero runtime deps)
├─ cordis.patch.yml        # inserts the tool-autotrans row (config.python: auto)
├─ lib/
│  └─ index.js             # cordis plugin: registers the `autotrans` tool
├─ python/
│  ├─ autotrans.py         # CLI entry (extract/translate/render/all + JSON summary)
│  ├─ core/                # config / extract / translate / render
│  ├─ glossaries/          # general / biomedical / anthropology TSV
│  └─ config.example.json
├─ scripts/
│  ├─ build-runtime.ps1    # package python core → self-contained runtime (Windows x64)
│  └─ get-runtime.ps1      # fetch the prebuilt runtime zip from a GitHub Release
├─ dist/                   # build output (git-ignored): autotrans/ + autotrans-win-x64.zip
├─ runtime/                # extracted runtime (git-ignored): autotrans.exe + _internal/
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
