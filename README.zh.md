# dsh-autotrans

一个 **DeepSeek Harness (DSH) 插件组合包**，用 DeepSeek API 把英文文献/专业书籍整本翻译成中文，
复用了久经考验的 AutoTrans 流水线：

```
PDF → 提取（结构化文本）→ 翻译（DeepSeek，术语表 + 断点续传）→ DOCX / EPUB
```

它作为普通 DSH 插件安装，注册一个面向模型工具 `autotrans`，让智能体可以直接翻译文档。
重活仍由内置的 Python 核心完成；Node 插件只是一个零依赖的薄进程封装。

[English](README.md)

## 功能

- **提取** —— 版面感知的 PDF 提取（单/双栏自动识别、标题识别、页眉页脚/图注/参考文献过滤）
  → `extracted.json` + 纯文本 `preview/`。
- **翻译** —— DeepSeek 批量翻译，术语表约束、逐段断点续传（重跑只补缺失块，不重复计费）、
  可配置并发。
- **渲染** —— 中文 `DOCX`（标准库生成，无需 python-docx）和/或 `EPUB`（用 ebooklib）。
- **术语表** —— 内置 `general`、`biomedical`、`anthropology`，或自备 TSV（每行 `英文<TAB>中文`）。

## 依赖

- 运行中的 DSH，且 `PATH` 里有 `pnpm`（供 `dsh plugin` 使用）。

插件运行用的解释器可以是三种之一（见下文「自带运行时」，或纯 Python 段）：

1. **预构建的自带运行时**（推荐）——设备上无需装 Python。
2. **系统 Python**（Python **3.10+**）并装好流水线依赖：

   ```sh
   pip install pymupdf ebooklib
   ```

   `pymupdf` 是提取必需；`ebooklib` 仅在输出 EPUB 时需要（DOCX 无需第三方包）。

## 自带运行时（无需系统 Python）

`scripts/build-runtime.ps1` 会把 AutoTrans 的 Python 核心**连同依赖**封装成 Windows x64
运行时，即使设备没装系统 Python 也能直接运行。插件 `config.python` 默认是 `auto`，会自动
使用放在 `<plugin>/runtime/autotrans.exe` 的运行时。

**先在本机构建一次**（需要装有 `pymupdf`、`ebooklib`、`pyinstaller` 的 Python）：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/build-runtime.ps1 -Python D:/Projects/CondaEnvs/booktrans/python.exe
# 产出 dist/autotrans/ 与 dist/autotrans-win-x64.zip（约 30 MB）
```

**发布**：把 `dist/autotrans-win-x64.zip` 作为本仓库某个 GitHub Release 的附件上传。
新设备拉取一次：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/get-runtime.ps1
# 从最新 release 下载 autotrans-win-x64.zip，并把 autotrans.exe 放到 <plugin>/runtime/，
# 插件的 "auto" python 模式会自动识别
```

之后无需改任何配置即可调用：

```
dist/autotrans/autotrans.exe --help
```

也可直接把 `config.python` 指向该 exe 路径（或含 `autotrans.exe` 的目录）以跳过自动检测。

## 装入 DSH

把本仓库推送到 GitHub 后，在每台设备上执行：

```sh
dsh plugin --profile web add github.com/<你的名字>/dsh-autotrans
# 然后重启该 profile（停掉并重新 `dsh web`），工具才会注册
```

任意 profile 均可（不止 `web`）：

```sh
dsh plugin --profile my-profile add github.com/<你的名字>/dsh-autotrans
```

`dsh plugin` 会把参数转发给 profile 目录内的 pnpm，随后对 `dsh.profile.bundles` 做对账：
由于本包声明了 `dsh.bundle.patch`，它会被自动追加为 profile 层。没有构建步骤，因此无需
`allowBuilds` 配置。

> 用本地检出代替 GitHub：
> `dsh plugin --profile web add file:<本仓库路径>`（或 `link:<路径>`）。

## 用法（在 DSH 中）

profile 重启后，智能体即拥有 `autotrans` 工具。让它翻译一个 PDF 即可，例如：

> 把 `D:\papers\paper.pdf` 翻译成中文 DOCX

工具参数：

| 参数 | 说明 |
|---|---|
| `pdf_path` | 文字型 PDF 的绝对路径（必填）。 |
| `action` | `all`（默认）/ `extract` / `translate` / `render`。 |
| `out_dir` | 输出目录（默认 `<pdf>_translated/`）。 |
| `glossary` | `general` / `biomedical` / `anthropology`，或 TSV 路径。 |
| `model` | DeepSeek 模型（默认 `deepseek-chat`）。 |
| `concurrency` | 并发请求数（默认 4）。 |
| `format` | `docx` / `epub` / `both`。 |
| `api_key` | DeepSeek API Key（否则读 `DEEPSEEK_API_KEY` 环境变量）。 |
| `config_path` | 高级选项的 JSON 配置文件路径。 |

在 DSH 运行环境中一次性设置 API Key（`DEEPSEEK_API_KEY=sk-...`），或每次调用通过
`api_key` 传入。

## 独立 CLI

内置的 Python 入口也可直接使用：

```sh
python python/autotrans.py all D:/papers/paper.pdf --glossary biomedical
python python/autotrans.py extract  paper.pdf   # 先看提取质量
python python/autotrans.py translate paper.pdf  # 需要 DEEPSEEK_API_KEY
python python/autotrans.py render   paper.pdf
```

产物默认写到 `<输出根>/autotrans/<PDF文件名（去扩展名）>/`：DSH 工具使用时输出根为
调用方的 agent 工作区；独立 CLI 时为 PDF 所在目录。内含 `extracted.json`、
`preview/article.txt`、`translated_final.json` 及渲染出的 `.docx`/`.epub`。
可用 `--out-dir` 覆盖。

## 配置

组合包的 `cordis.patch.yml` 插入一行带默认配置的行。在 profile 自己的 `cordis.patch.yml`
中按行 id 覆盖：

```yaml
- id: tool-autotrans
  config:
    python: auto              # auto=自带运行时，否则系统 python；或 exe 绝对路径
    model: deepseek-chat
    concurrency: 4
    format: docx
```

或禁用该工具：

```yaml
- id: tool-autotrans
  disabled: true
```

高级提取/翻译阈值放在 JSON 配置文件里（见 `python/config.example.json`）：提取几何参数、
批大小、temperature、自定义系统提示词、输出文件名等。

`translation.auto_glossary` 默认开启：翻译前会让模型先扫一遍当前文献，生成一张领域术语表
`auto_glossary.tsv`，再与你用 `--glossary`/配置选择的术语表合并（同名时以手写表优先）。
这是「换到全新研究领域也能自动统一译名」的机制；生成文件会缓存，删掉即可重新生成。

### 管线行为说明

- **不再句中硬截断**：超长段落只在**句子边界**处分块（不会把一句话/一个词从中间切断），
  单句即使超过块大小也整句翻译。
- **分级标题**：提取会为标题记一个 `level`（按编号/字号启发式：`1 Introduction`→1 级，
  `2.1 …`→2 级，…）；DOCX 渲染按级别用不同字号+加粗，使章/节/小节在视觉上拉开；
  EPUB 用 `h2`/`h3` 呈现。

## 目录结构

```
dsh-autotrans/
├─ package.json            # dsh.bundle.patch → cordis.patch.yml（零运行时依赖）
├─ cordis.patch.yml        # 插入 tool-autotrans 行（config.python: auto）
├─ lib/
│  └─ index.js             # cordis 插件：注册 `autotrans` 工具
├─ python/
│  ├─ autotrans.py         # CLI 入口（extract/translate/render/all + JSON 摘要）
│  ├─ core/                # config / extract / translate / render
│  ├─ glossaries/          # general / biomedical / anthropology TSV
│  └─ config.example.json
├─ scripts/
│  ├─ build-runtime.ps1    # 把 python 核心封装成自带运行时（Windows x64）
│  └─ get-runtime.ps1      # 从 GitHub Release 拉取预构建运行时
├─ dist/                   # 构建产物（git 忽略）：autotrans/ + autotrans-win-x64.zip
├─ runtime/                # 解压出的运行时（git 忽略）：autotrans.exe + _internal/
├─ requirements.txt
├─ README.md / README.zh.md
└─ LICENSE
```

## 说明

- **扫描版 PDF**：提取依赖文字层；请先 OCR（本工具不含 OCR）。
- **成本**：`deepseek-chat` 一篇文章通常只需几元人民币。
- **续传**：每块译完即写盘；重跑只补缺失块。

## 许可证

[MIT](LICENSE)
