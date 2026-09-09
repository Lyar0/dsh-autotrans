/**
 * dsh-autotrans — a DeepSeek Harness (DSH) plugin bundle.
 *
 * Registers one host-plane model tool, `autotrans`, that runs the bundled
 * Python pipeline (extract → translate → render) to translate an English PDF
 * into Chinese DOCX / EPUB. The plugin is a thin process wrapper: it owns no
 * LLM or PDF logic, it only resolves the bundled Python CLI, forwards a call,
 * and reports the outcome. The tool is effect-scoped, so it is removed when
 * the plugin is stopped or updated.
 *
 * Plugin contract (plain ESM, no build step):
 *   export const name / inject / apply(ctx, config)
 */
import { spawn } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join, resolve, basename } from "node:path";
import { fileURLToPath } from "node:url";

export const name = "autotrans";
export const inject = ["tools"];

/** This module lives in lib/; the bundled Python CLI sits in ../python. */
const PKG_DIR = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const PYTHON_DIR = join(PKG_DIR, "python");
const CLI = join(PYTHON_DIR, "autotrans.py");

/** A self-contained runtime extracted into <pkg>/runtime is auto-detected. */
const RUNTIME_EXE = join(PKG_DIR, "runtime", "autotrans.exe");

/**
 * Persistent per-user glossary library path (accumulates translated term pairs
 * across documents/domains). Kept under the DSH home so it survives plugin
 * reinstalls and obeys $DSH_HOME. Passed to the Python core via env
 * AUTOTRANS_USER_GLOSSARY.
 */
function userGlossaryPath() {
  const dshHome = (process.env.DSH_HOME || "").trim() || join(homedir(), ".dsh");
  return join(dshHome, "autotrans", "user_glossary.tsv");
}

/**
 * Read `DEEPSEEK_API_KEY` from DSH's own credentials file ($DSH_HOME/.credentials.yaml),
 * the same plaintext location DSH itself stores it. Used only when the key is neither
 * an explicit argument nor already present in the environment. Kept out of logs.
 */
function dshCredentialsFile() {
  const dshHome = (process.env.DSH_HOME || "").trim() || join(homedir(), ".dsh");
  return join(dshHome, ".credentials.yaml");
}

function readDshDeepseekKey() {
  const file = dshCredentialsFile();
  if (!existsSync(file)) return "";
  try {
    const text = readFileSync(file, "utf8");
    // Covers both a plain top-level `DEEPSEEK_API_KEY: <key>` and the DSH `refs:` nesting.
    const m = /^\s*(?:refs:)?\s*DEEPSEEK_API_KEY\s*:\s*["']?([^\s"']+)["']?\s*$/m.exec(text);
    if (m && m[1] && m[1].length > 0) return m[1];
  } catch {
    /* ignore unreadable credential file */
  }
  return "";
}

/**
 * Resolve the interpreter to spawn.
 *
 * Three forms of `config.python` are supported:
 *   - "auto" / empty / undefined → prefer the packaged runtime
 *     (<pkg>/runtime/autotrans.exe, produced by scripts/get-runtime.ps1) and fall
 *     back to a system `python` on PATH.
 *   - a path to an existing `autotrans.exe` / python binary → use it as-is.
 *   - a directory containing `autotrans.exe` → use that executable.
 *   - any other string → treated as a system interpreter name (e.g. "python3").
 */
function resolvePython(python) {
  const spec = typeof python === "string" && python.trim() ? python.trim() : "auto";
  if (spec === "auto") {
    return existsSync(RUNTIME_EXE) ? RUNTIME_EXE : "python";
  }
  if (existsSync(spec)) {
    // Pointed at a file (binary) → use it; pointed at a dir → find autotrans.exe.
    const isFile = !spec.endsWith("/") && !spec.endsWith("\\") && basename(spec).includes(".");
    const candidate = isFile
      ? spec
      : join(spec, /\.exe$/i.test(spec) ? "" : "autotrans.exe");
    if (!isFile && existsSync(candidate)) return candidate;
    return spec;
  }
  return spec;
}

/** The single summary line the Python CLI emits at the end of a run. */
const RESULT_PREFIX = "AUTOTRANS_RESULT=";

/** Tool schema (raw JSON Schema — the exact wire format the registry projects). */
const PARAMETERS = {
  type: "object",
  properties: {
    pdf_path: {
      type: "string",
      description:
        "Absolute path to the English PDF to translate. Must be a text-based PDF; scanned/image PDFs need OCR first.",
    },
    action: {
      type: "string",
      enum: ["all", "extract", "translate", "render"],
      description:
        "Which stage(s) to run. 'all' = extract + translate + render (default). Use 'extract' to inspect extraction quality before spending API credits.",
    },
    out_dir: {
      type: "string",
      description:
        "Optional output directory. Defaults to <pdf>_translated/ next to the PDF.",
    },
    glossary: {
      type: "string",
      description:
        "Terminology glossary: a built-in name (general/biomedical/anthropology) or an absolute path to a TSV file with English<TAB>Chinese rows. Keeps terminology consistent.",
    },
    model: {
      type: "string",
      description: "DeepSeek model name. Default deepseek-chat.",
    },
    concurrency: {
      type: "integer",
      description: "Number of concurrent translation requests. Default 4.",
    },
    format: {
      type: "string",
      enum: ["docx", "epub", "both"],
      description: "Output format(s) for the render stage. Default docx.",
    },
    api_key: {
      type: "string",
      description:
        "DeepSeek API key. Omit to read the DEEPSEEK_API_KEY environment variable of the DSH process.",
    },
    config_path: {
      type: "string",
      description:
        "Optional absolute path to a JSON config file with advanced options (extract thresholds, temperature, custom prompts).",
    },
  },
  required: ["pdf_path"],
};

const OUTPUT_SCHEMA = {
  type: "object",
  additionalProperties: false,
  properties: {
    ok: { type: "boolean" },
    message: { type: "string" },
    out_dir: { type: "string" },
    files: { type: "array", items: { type: "string" } },
    stdout: { type: "string" },
    stderr: { type: "string" },
  },
  required: ["ok", "message"],
};

/**
 * Run the bundled Python CLI and capture its complete stdout/stderr. Cancels
 * the child when `signal` aborts. Resolves (never rejects for a launched
 * process): a spawn failure reports as `spawnError`; otherwise `code`/`stdout`
 * /`stderr` are returned.
 */
function runPython(python, argv, { cwd = PKG_DIR, extraEnv = {}, signal } = {}) {
  return new Promise((resolveResult) => {
    let child;
    try {
      child = spawn(python, argv, {
        cwd,
        env: { ...process.env, PYTHONUTF8: "1", PYTHONIOENCODING: "utf-8", ...extraEnv },
        stdio: ["ignore", "pipe", "pipe"],
        windowsHide: true,
      });
    } catch (error) {
      resolveResult({ spawnError: error });
      return;
    }

    let stdout = "";
    let stderr = "";
    let settled = false;

    const settle = (result) => {
      if (settled) return;
      settled = true;
      resolveResult(result);
    };

    child.stdout.on("data", (d) => {
      stdout += d.toString("utf8");
    });
    child.stderr.on("data", (d) => {
      stderr += d.toString("utf8");
    });
    const onAbort = () => {
      try {
        child.kill();
      } catch {
        /* already gone */
      }
    };
    if (signal) {
      if (signal.aborted) onAbort();
      else signal.addEventListener("abort", onAbort, { once: true });
    }
    child.on("error", (error) => settle({ spawnError: error }));
    child.on("close", (code) => settle({ code: code ?? null, stdout, stderr }));
  });
}

/** Parse the trailing `AUTOTRANS_RESULT={json}` summary line out of stdout. */
function parseResult(stdout) {
  const line = stdout
    .split(/\r?\n/)
    .reverse()
    .find((l) => l.startsWith(RESULT_PREFIX));
  if (!line) return undefined;
  try {
    return JSON.parse(line.slice(RESULT_PREFIX.length));
  } catch {
    return undefined;
  }
}

/** A compact, model-facing summary of a completed run. */
function summarize(parsed, stdout, stderr) {
  if (parsed && typeof parsed.message === "string" && parsed.message) {
    return parsed.message;
  }
  const tail = stdout.trim().split(/\r?\n/).slice(-4).join("\n");
  return (tail || stderr.trim() || "finished").slice(0, 4000);
}

export function apply(ctx, config = {}) {
  const python = resolvePython(config.python);
  const defaults = {
    model: typeof config.model === "string" && config.model ? config.model : "deepseek-chat",
    concurrency: Number.isInteger(config.concurrency) && config.concurrency > 0 ? config.concurrency : 4,
    format: config.format === "epub" || config.format === "both" ? config.format : "docx",
  };

  const tool = {
    name: "autotrans",
    description:
      "Translate an English academic PDF/book into Chinese. Runs the AutoTrans pipeline: extracts text from the PDF, translates it in batches with the DeepSeek API (terminology-glossary constrained, resumable — re-running only fills missing segments), and renders Chinese DOCX/EPUB. Requires a text-based PDF and a DeepSeek API key (DEEPSEEK_API_KEY env var or the api_key argument). Use action='extract' first to check extraction quality.",
    parameters: PARAMETERS,
    output: {
      schema: OUTPUT_SCHEMA,
      render: (_args, value) => [{ type: "text", text: value.message }],
    },
    async execute(args, exec) {
      const pdfPath = typeof args.pdf_path === "string" ? args.pdf_path.trim() : "";
      if (!pdfPath) {
        return {
          ok: false,
          message: "autotrans: pdf_path is required (absolute path to a text-based PDF).",
          files: [],
          stdout: "",
          stderr: "",
        };
      }
      if (!existsSync(pdfPath)) {
        return {
          ok: false,
          message: `autotrans: PDF not found: ${pdfPath}`,
          files: [],
          stdout: "",
          stderr: "",
        };
      }

      const action = ["extract", "translate", "render"].includes(args.action) ? args.action : "all";
      const model = typeof args.model === "string" && args.model ? args.model : defaults.model;
      const concurrency =
        Number.isInteger(args.concurrency) && args.concurrency > 0 ? args.concurrency : defaults.concurrency;
      const format = ["docx", "epub", "both"].includes(args.format) ? args.format : defaults.format;

      // Session workspace root (mirrors dsh-tool-fs): the calling agent's session cwd.
      const workspace =
        exec?.agent?.session?.header?.cwd ||
        exec?.agent?.session?.header?.workspace ||
        process.cwd();
      // Default output: <workspace>/autotrans/<pdf-stem>/  (可被 out_dir 覆盖)
      let outDir = typeof args.out_dir === "string" && args.out_dir.trim() ? args.out_dir.trim() : "";
      if (!outDir) {
        const stem = (basename(pdfPath).replace(/\.pdf$/i, "") || "document")
          .replace(/[\\/:*?"<>|\u0000-\u001f]+/g, "_");
        outDir = join(workspace, "autotrans", stem);
      }

      const argv = [CLI, action, pdfPath, "--out-dir", outDir, "--model", model, "--concurrency", String(concurrency), "--format", format];
      const push = (flag, value) => {
        if (value !== undefined && value !== null && value !== "") argv.push(flag, String(value));
      };
      push("--glossary", args.glossary);
      push("--config", args.config_path);

      const extraEnv = {};
      // 持久用户词表库：放在 DSH 主目录，跨次/跨域累积，重装插件不丢
      extraEnv.AUTOTRANS_USER_GLOSSARY = userGlossaryPath();
      // Key 供给优先级：显式 api_key 参数 > 继承的环境 > DSH 凭据文件(.credentials.yaml)
      const supplied = (typeof args.api_key === "string" && args.api_key.trim())
        ? args.api_key.trim()
        : (process.env.DEEPSEEK_API_KEY || "").trim();
      if (supplied) {
        extraEnv.DEEPSEEK_API_KEY = supplied;
      } else {
        const fromCreds = readDshDeepseekKey();
        if (fromCreds) extraEnv.DEEPSEEK_API_KEY = fromCreds;
      }

      const result = await runPython(python, argv, { cwd: workspace, extraEnv, signal: exec?.signal });

      if (result.spawnError) {
        return {
          ok: false,
          message:
            `autotrans: failed to launch Python interpreter "${python}": ${result.spawnError.message}. ` +
            `Install Python 3.10+, then \`pip install pymupdf ebooklib\`, and set the interpreter via the profile's ` +
            `cordis.patch.yml (row id tool-autotrans, config.python).`,
          files: [],
          stdout: "",
          stderr: String(result.spawnError),
        };
      }

      const parsed = parseResult(result.stdout);
      const succeeded = result.code === 0 && (!parsed || parsed.ok !== false);
      return {
        ok: succeeded,
        message: summarize(parsed, result.stdout, result.stderr),
        ...(parsed && typeof parsed.out_dir === "string" ? { out_dir: parsed.out_dir } : {}),
        files: parsed && Array.isArray(parsed.files) ? parsed.files.map(String) : [],
        stdout: result.stdout.slice(-8000),
        stderr: result.stderr.slice(-8000),
      };
    },
  };

  ctx.tools.register(tool);
}
