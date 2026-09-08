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
- Python **3.10+**，且插件所调用的解释器（默认 `python`）装好流水线依赖：

  ```sh
  pip install pymupdf ebooklib
  ```

  `pymupdf` 是提取必需；`ebooklib` 仅在输出 EPUB 时需要（DOCX 无需第三方包）。

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

产物在 `<pdf>_translated/`：`extracted.json`、`preview/article.txt`、
`translated_final.json`，以及渲染出的 `.docx`/`.epub`。

## 配置

组合包的 `cordis.patch.yml` 插入一行带默认配置的行。在 profile 自己的 `cordis.patch.yml`
中按行 id 覆盖：

```yaml
- id: tool-autotrans
  config:
    python: python3            # 已装 pymupdf + ebooklib 的解释器
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

## 目录结构

```
dsh-autotrans/
├─ package.json            # dsh.bundle.patch → cordis.patch.yml（零运行时依赖）
├─ cordis.patch.yml        # 插入 tool-autotrans 行
├─ lib/
│  └─ index.js             # cordis 插件：注册 `autotrans` 工具
├─ python/
│  ├─ autotrans.py         # CLI 入口（extract/translate/render/all + JSON 摘要）
│  ├─ core/                # config / extract / translate / render
│  ├─ glossaries/          # general / biomedical / anthropology TSV
│  └─ config.example.json
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
