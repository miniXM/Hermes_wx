const { app, BrowserWindow, dialog, ipcMain, shell } = require("electron");
const { spawn, execFile } = require("child_process");
const fs = require("fs");
const net = require("net");
const path = require("path");

app.setName("hermes-wecom-plugin");
app.setPath("userData", path.join(app.getPath("appData"), "hermes-wecom-plugin"));

const isDev = !app.isPackaged;
const rootDir = isDev ? path.resolve(__dirname, "..") : path.dirname(app.getPath("exe"));
const cliExe = path.join(rootDir, "cli.exe");
const loaderDll = path.join(rootDir, "loader.dll");
const helperDll = path.join(rootDir, "helper.dll");
const hermesPluginSourceCandidates = [
  path.join(rootDir, "hermes_plugins", "wecom_pc_hook"),
  path.resolve(__dirname, "..", "hermes_plugins", "wecom_pc_hook"),
];
const legacyConfigPath = path.join(rootDir, "plugin-config.json");
const DEFAULT_HERMES_BASE_URL = "http://127.0.0.1:8642/v1";
const DEFAULT_HERMES_API_KEY = "change-me-local-dev";
const DEFAULT_BOT_HOOK_TOKEN = "testtoken";
const DEFAULT_HOME_CHANNEL_NAME = "WeCom Home";
const ADAPTER_PORT = 8001;
const HERMES_PLATFORM_PLUGIN = "wecom_pc_hook";
const DEFAULT_DISABLED_TOOLSETS = "";
const CHAT_ONLY_DISABLED_TOOLSETS = [
  "terminal",
  "file",
  "web",
  "browser",
  "browser-cdp",
  "computer_use",
  "code_execution",
  "cronjob",
  "delegation",
  "moa",
  "session_search",
  "skills",
  "memory",
  "todo",
  "kanban",
  "messaging",
  "image_gen",
  "video_gen",
  "vision",
  "video",
  "tts",
  "x_search",
];
const WXWORK_TESTED_VERSION = "4.1.33.6009";
const WXWORK_INSTALL_ROOTS = [
  "C:\\Program Files (x86)\\WXWork",
  "C:\\Program Files\\WXWork",
];

let mainWindow = null;
let cliProcess = null;
let hermesProcess = null;
let hermesStartedByPlugin = false;
let pluginDesiredRunning = false;
const iconCache = new Map();
const fileVersionCache = new Map();
let elevatedCache = null;
let cliLogTailTimer = null;
let cliLogTailPath = "";
let cliLogTailOffset = 0;
let cliLogCarry = "";
let lastChatLogKey = "";
let cliRuntime = {
  logPath: "",
  connectedAt: 0,
  disconnectedAt: 0,
  injectResult: null,
  injectAt: 0,
  receivedAt: 0,
  outboundAt: 0,
  pollErrorAt: 0,
  sendErrorAt: 0,
  lastError: "",
};

function normalizeHermesBaseUrl(value) {
  const raw = String(value || DEFAULT_HERMES_BASE_URL).trim();
  const withScheme = /^[a-z][a-z\d+.-]*:\/\//i.test(raw) ? raw : `http://${raw}`;
  try {
    const url = new URL(withScheme);
    if (!url.pathname || url.pathname === "/") {
      url.pathname = "/v1";
    }
    return url.toString().replace(/\/+$/, "");
  } catch {
    return DEFAULT_HERMES_BASE_URL;
  }
}

function getHermesEndpoint(config) {
  const baseUrl = normalizeHermesBaseUrl(config?.hermesBaseUrl);
  const url = new URL(baseUrl);
  const port = Number(url.port) || (url.protocol === "https:" ? 443 : 80);
  return {
    baseUrl,
    host: url.hostname,
    port,
    protocol: url.protocol,
    modelsUrl: `${baseUrl}/models`,
  };
}

function isLocalHermesEndpoint(endpoint) {
  return ["127.0.0.1", "localhost", "::1"].includes(endpoint.host);
}

const defaultConfig = () => ({
  wxworkExe: firstExistingPath([
    "C:\\Program Files (x86)\\WXWork\\WXWork.exe",
    "C:\\Program Files\\WXWork\\WXWork.exe",
  ]),
  hermesBaseUrl: DEFAULT_HERMES_BASE_URL,
  hermesApiKey: DEFAULT_HERMES_API_KEY,
  botHookToken: DEFAULT_BOT_HOOK_TOKEN,
  replyMode: "all",
  wakeWords: "@Hermes,Hermes",
  allowAllUsers: true,
  toolPolicyMode: "native",
  disabledToolsets: DEFAULT_DISABLED_TOOLSETS,
});

const parseCsvList = (value) =>
  String(value || "")
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);

const normalizeCsvList = (value) => parseCsvList(value).join(",");

const normalizeToolPolicyMode = (value) =>
  ["native", "chat", "custom"].includes(value) ? value : "native";

const disabledToolsetsForMode = (mode, customValue = "") => {
  const normalizedMode = normalizeToolPolicyMode(mode);
  if (normalizedMode === "chat") {
    return CHAT_ONLY_DISABLED_TOOLSETS.join(",");
  }
  if (normalizedMode === "custom") {
    return normalizeCsvList(customValue);
  }
  return "";
};

const normalizeConfig = (config) => {
  const replyMode = ["all", "wake", "off"].includes(config.replyMode)
    ? config.replyMode
    : "all";
  const toolPolicyMode = normalizeToolPolicyMode(config.toolPolicyMode);
  return {
    wxworkExe: String(config.wxworkExe || ""),
    hermesBaseUrl: normalizeHermesBaseUrl(config.hermesBaseUrl),
    hermesApiKey: String(config.hermesApiKey || DEFAULT_HERMES_API_KEY),
    botHookToken: String(config.botHookToken || DEFAULT_BOT_HOOK_TOKEN),
    replyMode,
    wakeWords: String(config.wakeWords || "@Hermes,Hermes"),
    allowAllUsers: config.allowAllUsers !== false,
    toolPolicyMode,
    disabledToolsets: disabledToolsetsForMode(toolPolicyMode, config.disabledToolsets),
  };
};

function firstExistingPath(candidates) {
  return candidates.find((candidate) => candidate && fileExists(candidate)) || "";
}

function getConfigPath() {
  return path.join(app.getPath("userData"), "plugin-config.json");
}

const loadConfig = () => {
  const defaults = defaultConfig();
  for (const candidate of [getConfigPath(), legacyConfigPath]) {
    try {
      const saved = JSON.parse(fs.readFileSync(candidate, "utf8"));
      if (candidate === legacyConfigPath) {
        try {
          fs.mkdirSync(path.dirname(getConfigPath()), { recursive: true });
          fs.writeFileSync(
            getConfigPath(),
            JSON.stringify(normalizeConfig({ ...defaults, ...saved }), null, 2),
            "utf8",
          );
        } catch {
        }
      }
      return normalizeConfig({ ...defaults, ...saved });
    } catch {
    }
  }
  return normalizeConfig(defaults);
};

const saveConfig = (nextConfig) => {
  const merged = normalizeConfig({ ...loadConfig(), ...nextConfig });
  fs.mkdirSync(path.dirname(getConfigPath()), { recursive: true });
  fs.writeFileSync(getConfigPath(), JSON.stringify(merged, null, 2), "utf8");
  emitLog("Configuration saved.");
  return merged;
};

const copyDirectoryRecursive = (sourceDir, targetDir) => {
  if (!fileExists(sourceDir)) {
    throw new Error(`Hermes adapter source missing: ${sourceDir}`);
  }
  fs.mkdirSync(targetDir, { recursive: true });
  for (const entry of fs.readdirSync(sourceDir, { withFileTypes: true })) {
    const source = path.join(sourceDir, entry.name);
    const target = path.join(targetDir, entry.name);
    if (entry.isDirectory()) {
      copyDirectoryRecursive(source, target);
    } else {
      fs.copyFileSync(source, target);
    }
  }
};

const writeBotIni = (config) => {
  const content = `[config]\r\nhook = ${adapterHookUrl(config)}\r\napi = http://127.0.0.1:${ADAPTER_PORT}\r\n`;
  fs.writeFileSync(path.join(rootDir, "bot.ini"), content, "utf8");
};

const writeHermesAdapterConfigScript = () => {
  const scriptPath = path.join(app.getPath("userData"), "configure-wecom-pc-hook.py");
  const script = String.raw`
from __future__ import annotations

import os
import json
import sys
from pathlib import Path

import yaml

cfg_path = Path(sys.argv[1])
payload_arg = sys.argv[2] if len(sys.argv) > 2 else "{}"
if payload_arg.startswith("@"):
    payload = json.loads(Path(payload_arg[1:]).read_text(encoding="utf-8"))
else:
    payload = json.loads(payload_arg)

if cfg_path.exists():
    try:
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except Exception:
        cfg = {}
else:
    cfg = {}

plugins = cfg.setdefault("plugins", {})
enabled = plugins.setdefault("enabled", [])
if not isinstance(enabled, list):
    enabled = []
plugins["enabled"] = enabled
if "wecom_pc_hook" not in enabled:
    enabled.append("wecom_pc_hook")

disabled = plugins.setdefault("disabled", [])
if isinstance(disabled, list) and "wecom_pc_hook" in disabled:
    plugins["disabled"] = [item for item in disabled if item != "wecom_pc_hook"]

platforms = cfg.setdefault("platforms", {})
platform = platforms.setdefault("wecom_pc_hook", {})
platform["enabled"] = True
home_channel = str(payload.get("homeChannel") or "").strip()
if home_channel:
    platform["home_channel"] = {
        "platform": "wecom_pc_hook",
        "chat_id": home_channel,
        "name": str(payload.get("homeChannelName") or "WeCom Home"),
        "thread_id": None,
    }
extra = platform.setdefault("extra", {})
extra.update(
    {
        "enabled": True,
        "host": "127.0.0.1",
        "port": int(payload["port"]),
        "token": payload["token"],
        "reply_mode": payload["replyMode"],
        "wake_words": payload["wakeWords"],
        "allow_all_users": bool(payload.get("allowAllUsers", True)),
    }
)
agent = cfg.setdefault("agent", {})
disabled_toolsets = payload.get("disabledToolsets") or []
if isinstance(disabled_toolsets, str):
    disabled_toolsets = [item.strip() for item in disabled_toolsets.split(",") if item.strip()]
if disabled_toolsets:
    agent["disabled_toolsets"] = disabled_toolsets
else:
    agent["disabled_toolsets"] = []
if home_channel and not os.environ.get("WECOM_PC_HOOK_HOME_CHANNEL"):
    cfg["WECOM_PC_HOOK_HOME_CHANNEL"] = home_channel
    cfg["WECOM_PC_HOOK_HOME_CHANNEL_NAME"] = str(payload.get("homeChannelName") or "WeCom Home")
cfg_path.parent.mkdir(parents=True, exist_ok=True)
cfg_path.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
`;
  fs.mkdirSync(path.dirname(scriptPath), { recursive: true });
  fs.writeFileSync(scriptPath, script.trimStart(), "utf8");
  return scriptPath;
};

const readChannelDirectory = () => {
  const file = path.join(hermesHomeDir(), "channel_directory.json");
  try {
    const parsed = JSON.parse(fs.readFileSync(file, "utf8"));
    const targets = parsed?.platforms?.[HERMES_PLATFORM_PLUGIN];
    return Array.isArray(targets) ? targets : [];
  } catch {
    return [];
  }
};

const resolveWeComHomeChannel = async () => {
  const targets = readChannelDirectory();
  const current = targets
    .map((target) => ({
      id: String(target?.id || "").trim(),
      name: String(target?.name || "").trim(),
    }))
    .filter((target) => target.id && !target.id.startsWith("S:codex-"));
  return current[current.length - 1] || null;
};

const configureHermesAdapter = async (config, hermesCommand) => {
  const configPath =
    (await execPowerShell(`& '${escapePowerShell(hermesCommand)}' config path`, 10000)
      .then(({ stdout }) => stdout.trim())
      .catch(() => path.join(hermesHomeDir(), "config.yaml"))) || path.join(hermesHomeDir(), "config.yaml");
  const scriptPath = writeHermesAdapterConfigScript();
  const pythonCommand = path.join(path.dirname(hermesCommand), "python.exe");
  const pythonExe = fileExists(pythonCommand) ? pythonCommand : "python";
  const homeChannel = await resolveWeComHomeChannel();
  const payload = {
    port: ADAPTER_PORT,
    token: config.botHookToken || DEFAULT_BOT_HOOK_TOKEN,
    replyMode: config.replyMode || "all",
    wakeWords: config.wakeWords || "@Hermes,Hermes",
    allowAllUsers: config.allowAllUsers !== false,
    disabledToolsets: parseCsvList(config.disabledToolsets),
    homeChannel: homeChannel?.id || "",
    homeChannelName: homeChannel?.name || DEFAULT_HOME_CHANNEL_NAME,
  };
  const payloadPath = path.join(app.getPath("userData"), "wecom-pc-hook-config-payload.json");
  fs.writeFileSync(payloadPath, JSON.stringify(payload), "utf8");
  await execPowerShell(
    `& '${escapePowerShell(hermesCommand)}' plugins enable ${HERMES_PLATFORM_PLUGIN}`,
    20000,
  ).catch(() => undefined);
  await execPowerShell(
    `& '${escapePowerShell(pythonExe)}' '${escapePowerShell(scriptPath)}' '${escapePowerShell(configPath)}' '@${escapePowerShell(payloadPath)}'`,
    20000,
  );
};

const ensureHermesAdapter = async (config, hermesCommand = null) => {
  copyDirectoryRecursive(getHermesPluginSourceDir(), hermesUserPluginDir());
  writeBotIni(config);
  const command = hermesCommand || (await findHermesCommand());
  if (!command) {
    throw new Error("没有找到 hermes 命令，无法启用 WeCom PC Hook adapter。");
  }
  await configureHermesAdapter(config, command);
  emitLog(`Hermes WeCom PC Hook adapter 已配置：${adapterHookUrl(config)}`);
};

const runLogsDir = () => {
  const dir = path.join(rootDir, "logs");
  fs.mkdirSync(dir, { recursive: true });
  return dir;
};

const adapterHookUrl = (config = loadConfig()) =>
  `http://127.0.0.1:${ADAPTER_PORT}/hook/${encodeURIComponent(
    config.botHookToken || DEFAULT_BOT_HOOK_TOKEN,
  )}`;

const hermesHomeDir = () =>
  path.join(process.env.LOCALAPPDATA || path.dirname(app.getPath("appData")), "hermes");

const hermesUserPluginDir = () =>
  path.join(hermesHomeDir(), "plugins", HERMES_PLATFORM_PLUGIN);

const hermesSoulPath = () => path.join(hermesHomeDir(), "SOUL.md");

const ensureHermesSoulFile = () => {
  const file = hermesSoulPath();
  if (!fileExists(file)) {
    fs.mkdirSync(path.dirname(file), { recursive: true });
    fs.writeFileSync(
      file,
      [
        "# Hermes Personality",
        "",
        "Describe the assistant personality, tone, boundaries, and working style here.",
        "Hermes reads this file as the global SOUL.md persona.",
        "",
      ].join("\n"),
      "utf8",
    );
  }
  return file;
};

const openTextEditor = (file) =>
  new Promise((resolve, reject) => {
    const child = spawn("notepad.exe", [file], {
      detached: true,
      stdio: "ignore",
      windowsHide: false,
    });
    child.once("error", reject);
    child.unref();
    resolve();
  });

const stamp = () => new Date().toISOString().replace(/[:.]/g, "-");

const emitLog = (message) => {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send("log", {
      at: new Date().toLocaleTimeString(),
      message,
    });
  }
};

const compactText = (value, max = 260) => {
  const text = String(value || "").replace(/\s+/g, " ").trim();
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
};

const extractJsonAfter = (line, marker) => {
  const index = line.indexOf(marker);
  if (index < 0) {
    return null;
  }
  const raw = line.slice(index + marker.length).trim();
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
};

const describeIncomingChat = (payload) => {
  const data = payload?.data || {};
  const sender = data.sender_name || data.sender || "未知";
  const conversation = data.conversation_id || payload?.conversation_id || "";
  const isSelf = Number(data.is_pc) === 1;
  const prefix = isSelf ? "企微发出" : "企微收到";
  if (payload?.type === 11041) {
    const content = compactText(data.content || "");
    if (!content) {
      return "";
    }
    return `${prefix}｜${sender}｜${conversation}｜${content}`;
  }
  if (payload?.type === 11042) {
    const cdn = data.cdn || {};
    const kind = Number(data.content_type) === 14 ? "图片" : "媒体";
    const size = cdn.size ? `，${cdn.size} bytes` : "";
    const dimensions = cdn.width && cdn.height ? `，${cdn.width}x${cdn.height}` : "";
    return `${prefix}｜${sender}｜${conversation}｜[${kind}${dimensions}${size}]`;
  }
  return "";
};

const describeOutboundChat = (payload) => {
  const data = payload?.data || {};
  const conversation = data.conversation_id || payload?.conversation_id || "";
  if (payload?.type === 11029) {
    return `插件下发｜${conversation}｜${compactText(data.content || "")}`;
  }
  if ([11030, 11031, 11067].includes(Number(payload?.type))) {
    const kindByType = { 11030: "图片", 11031: "文件", 11067: "视频" };
    const file = data.file_name || path.basename(data.file_path || data.path || data.file || "");
    return `插件下发｜${conversation}｜[${kindByType[payload.type] || "媒体"}] ${compactText(file || data.file_path || data.path || "")}`;
  }
  return "";
};

const emitChatLog = (message) => {
  if (!message) {
    return;
  }
  if (message === lastChatLogKey) {
    return;
  }
  lastChatLogKey = message;
  emitLog(message);
};

const parseCliLogLine = (line) => {
  const now = Date.now();
  if (line.includes("[CONNECTED]")) {
    cliRuntime.connectedAt = now;
  }
  if (line.includes("[DISCONNECTED]")) {
    cliRuntime.disconnectedAt = now;
  }
  if (line.includes("[INJECT RESULT]")) {
    const match = line.match(/\[INJECT RESULT\]\s*(-?\d+)/);
    cliRuntime.injectResult = match ? Number(match[1]) : null;
    cliRuntime.injectAt = now;
  }
  if (line.includes("[POLL ERROR]") || line.includes("[CURL ERROR]")) {
    cliRuntime.pollErrorAt = now;
    cliRuntime.lastError = compactText(line, 220);
  }
  if (line.includes("[ERROR]")) {
    cliRuntime.sendErrorAt = now;
    cliRuntime.lastError = compactText(line, 220);
  }
  if (line.includes("[RECEIVED MSG]")) {
    cliRuntime.receivedAt = now;
    emitChatLog(describeIncomingChat(extractJsonAfter(line, "[RECEIVED MSG]")));
    return;
  }
  if (line.includes("[POLL RESPONSE]")) {
    cliRuntime.outboundAt = now;
    emitChatLog(describeOutboundChat(extractJsonAfter(line, "[POLL RESPONSE]")));
  }
};

const latestCliLogPath = () => {
  try {
    const files = fs
      .readdirSync(runLogsDir(), { withFileTypes: true })
      .filter((entry) => entry.isFile() && /^cli-.*\.out\.log$/i.test(entry.name))
      .map((entry) => {
        const fullPath = path.join(runLogsDir(), entry.name);
        return { fullPath, mtimeMs: fs.statSync(fullPath).mtimeMs };
      })
      .sort((a, b) => b.mtimeMs - a.mtimeMs);
    return files[0]?.fullPath || "";
  } catch {
    return "";
  }
};

const resetCliRuntimeForLog = (logPath) => {
  if (cliRuntime.logPath === logPath) {
    return;
  }
  cliRuntime = {
    logPath,
    connectedAt: 0,
    disconnectedAt: 0,
    injectResult: null,
    injectAt: 0,
    receivedAt: 0,
    outboundAt: 0,
    pollErrorAt: 0,
    sendErrorAt: 0,
    lastError: "",
  };
};

const seedCliChatLog = (logPath) => {
  try {
    resetCliRuntimeForLog(logPath);
    const stat = fs.statSync(logPath);
    const tailBytes = Math.min(stat.size, 48 * 1024);
    const fd = fs.openSync(logPath, "r");
    const buffer = Buffer.alloc(tailBytes);
    fs.readSync(fd, buffer, 0, tailBytes, stat.size - tailBytes);
    fs.closeSync(fd);
    buffer
      .toString("utf8")
      .split(/\r?\n/)
      .forEach(parseCliLogLine);
  } catch (error) {
    logError("seed-cli-chat-log", error);
  }
};

const pollCliChatLog = () => {
  const logPath = latestCliLogPath();
  if (!logPath) {
    return;
  }
  if (logPath !== cliLogTailPath) {
    resetCliRuntimeForLog(logPath);
    cliLogTailPath = logPath;
    cliLogCarry = "";
    try {
      seedCliChatLog(logPath);
      cliLogTailOffset = fs.statSync(logPath).size;
    } catch {
      cliLogTailOffset = 0;
    }
    return;
  }
  try {
    const stat = fs.statSync(logPath);
    if (stat.size < cliLogTailOffset) {
      cliLogTailOffset = 0;
      cliLogCarry = "";
    }
    if (stat.size === cliLogTailOffset) {
      return;
    }
    const fd = fs.openSync(logPath, "r");
    const buffer = Buffer.alloc(stat.size - cliLogTailOffset);
    fs.readSync(fd, buffer, 0, buffer.length, cliLogTailOffset);
    fs.closeSync(fd);
    cliLogTailOffset = stat.size;
    const text = cliLogCarry + buffer.toString("utf8");
    const lines = text.split(/\r?\n/);
    cliLogCarry = lines.pop() || "";
    lines.forEach(parseCliLogLine);
  } catch (error) {
    logError("tail-cli-chat-log", error);
  }
};

const startCliChatLogTail = () => {
  if (cliLogTailTimer) {
    return;
  }
  pollCliChatLog();
  cliLogTailTimer = setInterval(pollCliChatLog, 900);
};

const logError = (label, error) => {
  try {
    const message = error instanceof Error ? `${error.stack || error.message}` : String(error);
    fs.appendFileSync(
      path.join(runLogsDir(), "electron-main-error.log"),
      `[${new Date().toISOString()}] ${label}: ${message}\n`,
      "utf8",
    );
  } catch {
  }
};

const ipcSafe = (label, handler) => async (...args) => {
  try {
    return await handler(...args);
  } catch (error) {
    logError(label, error);
    throw error;
  }
};

const fileExists = (file) => {
  try {
    return fs.existsSync(file);
  } catch {
    return false;
  }
};

const getHermesPluginSourceDir = () =>
  firstExistingPath(hermesPluginSourceCandidates) || hermesPluginSourceCandidates[0];

const getFileIconDataUrl = async (file) => {
  if (!file || !fileExists(file)) {
    return null;
  }

  const cacheKey = normalize(file);
  if (iconCache.has(cacheKey)) {
    return iconCache.get(cacheKey);
  }

  try {
    const image = await app.getFileIcon(file, { size: "large" });
    const dataUrl = image.isEmpty() ? null : image.toDataURL();
    iconCache.set(cacheKey, dataUrl);
    return dataUrl;
  } catch {
    iconCache.set(cacheKey, null);
    return null;
  }
};

const isPortOpen = (port) =>
  new Promise((resolve) => {
    const socket = new net.Socket();
    const done = (open) => {
      socket.removeAllListeners();
      socket.destroy();
      resolve(open);
    };
    socket.setTimeout(350);
    socket.once("connect", () => done(true));
    socket.once("timeout", () => done(false));
    socket.once("error", () => done(false));
    socket.connect(port, "127.0.0.1");
  });

const findProcesses = (name) =>
  new Promise((resolve) => {
    execFile(
      "powershell.exe",
      [
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        `Get-CimInstance Win32_Process | Where-Object { $_.Name -eq '${name}' } | Select-Object ProcessId,Name,ExecutablePath,CommandLine | ConvertTo-Json -Compress`,
      ],
      { windowsHide: true, timeout: 6000 },
      (_error, stdout) => {
        const text = stdout.trim();
        if (!text) {
          resolve([]);
          return;
        }
        try {
          const parsed = JSON.parse(text);
          resolve(Array.isArray(parsed) ? parsed : [parsed]);
        } catch {
          resolve([]);
        }
      },
    );
  });

const normalize = (value) => path.normalize(value || "").toLowerCase();

const execPowerShell = (command, timeout = 15000) =>
  new Promise((resolve, reject) => {
    execFile(
      "powershell.exe",
      ["-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
      { windowsHide: true, timeout, maxBuffer: 1024 * 1024 * 8 },
      (error, stdout, stderr) => {
        if (error) {
          const details = stderr?.trim() || stdout?.trim() || error.message;
          reject(new Error(details));
          return;
        }
        resolve({ stdout: stdout || "", stderr: stderr || "" });
      },
    );
  });

const escapePowerShell = (value) => String(value || "").replace(/'/g, "''");

const parseJson = (text, fallback = null) => {
  const trimmed = String(text || "").trim();
  if (!trimmed) {
    return fallback;
  }
  try {
    return JSON.parse(trimmed);
  } catch {
    return fallback;
  }
};

const getFileVersion = async (file) => {
  if (!file || !fileExists(file)) {
    return null;
  }
  let statKey = "";
  try {
    const stat = fs.statSync(file);
    statKey = `${normalize(file)}:${stat.size}:${Number(stat.mtimeMs)}`;
    if (fileVersionCache.has(statKey)) {
      return fileVersionCache.get(statKey);
    }
  } catch {
  }
  try {
    const { stdout } = await execPowerShell(
      `$item = Get-Item -LiteralPath '${escapePowerShell(file)}' -ErrorAction Stop; $item.VersionInfo.FileVersion`,
      5000,
    );
    const version = stdout.trim() || null;
    if (statKey) {
      fileVersionCache.set(statKey, version);
    }
    return version;
  } catch {
    return null;
  }
};

const isTestedWxWorkVersion = (version) =>
  String(version || "").startsWith(WXWORK_TESTED_VERSION);

const wxworkUpgradeRoot = () =>
  path.join(app.getPath("appData"), "Tencent", "WXWork", "upgrade");

const getWxWorkServiceInfo = async () => {
  try {
    const { stdout } = await execPowerShell(
      "Get-CimInstance Win32_Service -Filter \"Name='WXWorkUpgrader'\" | Select-Object Name,State,StartMode,PathName | ConvertTo-Json -Compress",
      6000,
    );
    const service = parseJson(stdout, null);
    if (!service) {
      return { exists: false, state: "Missing", startMode: "Missing", path: "" };
    }
    return {
      exists: true,
      state: service.State || "",
      startMode: service.StartMode || "",
      path: service.PathName || "",
    };
  } catch {
    return { exists: false, state: "Unknown", startMode: "Unknown", path: "" };
  }
};

const getWxWorkInstallInfo = async () => {
  const roots = [];
  for (const root of WXWORK_INSTALL_ROOTS) {
    if (!fileExists(root)) {
      continue;
    }
    const rootExe = path.join(root, "WXWork.exe");
    const versionDirs = [];
    try {
      for (const entry of fs.readdirSync(root, { withFileTypes: true })) {
        if (!entry.isDirectory() || !/^\d+\.\d+\.\d+\.\d+$/.test(entry.name)) {
          continue;
        }
        const exe = path.join(root, entry.name, "WXWork.exe");
        versionDirs.push({
          version: entry.name,
          path: exe,
          hasExe: fileExists(exe),
          fileVersion: await getFileVersion(exe),
        });
      }
    } catch {
    }
    roots.push({
      root,
      rootExe,
      rootExeExists: fileExists(rootExe),
      rootExeVersion: await getFileVersion(rootExe),
      versionDirs,
    });
  }
  return roots;
};

const getWxWorkUpgradeInfo = async () => {
  const root = wxworkUpgradeRoot();
  const parent = path.dirname(root);
  const disabledCaches = [];
  const cachedVersions = [];

  try {
    if (fileExists(parent)) {
      for (const entry of fs.readdirSync(parent, { withFileTypes: true })) {
        if (entry.isDirectory() && /^upgrade\.disabled-\d+/.test(entry.name)) {
          disabledCaches.push(path.join(parent, entry.name));
        }
      }
    }
  } catch {
  }

  try {
    if (fileExists(root)) {
      for (const entry of fs.readdirSync(root, { withFileTypes: true })) {
        if (entry.isDirectory() && /^\d+\.\d+\.\d+\.\d+$/.test(entry.name)) {
          cachedVersions.push(entry.name);
        }
      }
    }
  } catch {
  }

  const rootExe = path.join(root, "WXWork.exe");
  return {
    root,
    exists: fileExists(root),
    rootExe,
    rootExeExists: fileExists(rootExe),
    rootExeVersion: await getFileVersion(rootExe),
    cachedVersions,
    disabledCaches,
  };
};

const isCurrentProcessElevated = async () => {
  if (elevatedCache !== null) {
    return elevatedCache;
  }
  try {
    const { stdout } = await execPowerShell(
      "[Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)",
      5000,
    );
    elevatedCache = stdout.trim().toLowerCase() === "true";
  } catch {
    elevatedCache = false;
  }
  return elevatedCache;
};

const writeFreezeWxWorkScript = () => {
  const scriptPath = path.join(app.getPath("userData"), "freeze-wxwork-updates.ps1");
  const upgradeRoot = wxworkUpgradeRoot();
  const installRoots = WXWORK_INSTALL_ROOTS.map((root) => `'${escapePowerShell(root)}'`).join(", ");
  const script = `
$ErrorActionPreference = "Continue"
try {
  Stop-Service -Name "WXWorkUpgrader" -Force -ErrorAction SilentlyContinue
} catch {}

try {
  Set-Service -Name "WXWorkUpgrader" -StartupType Disabled -ErrorAction Stop
} catch {
  sc.exe config WXWorkUpgrader start= disabled | Out-Null
}

try {
  reg add "HKLM\\SYSTEM\\CurrentControlSet\\Services\\WXWorkUpgrader" /v Start /t REG_DWORD /d 4 /f | Out-Null
} catch {}

$upgradeRoot = '${escapePowerShell(upgradeRoot)}'
if (Test-Path -LiteralPath $upgradeRoot) {
  $target = Join-Path (Split-Path -Parent $upgradeRoot) ("upgrade.disabled-" + (Get-Date -Format "yyyyMMddHHmmss"))
  Move-Item -LiteralPath $upgradeRoot -Destination $target -Force
}

foreach ($root in @(${installRoots})) {
  $updaterDir = Join-Path $root "WXWorkUpgrader"
  if (Test-Path -LiteralPath $updaterDir) {
    $target = "$updaterDir.disabled-" + (Get-Date -Format "yyyyMMddHHmmss")
    Move-Item -LiteralPath $updaterDir -Destination $target -Force
  }
}
`;
  fs.mkdirSync(path.dirname(scriptPath), { recursive: true });
  fs.writeFileSync(scriptPath, script.trim(), "utf8");
  return scriptPath;
};

const runElevatedFreezeWxWorkUpdates = async () => {
  const scriptPath = writeFreezeWxWorkScript();
  await execPowerShell(
    `$args = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", '${escapePowerShell(scriptPath)}'); Start-Process -FilePath "powershell.exe" -ArgumentList $args -Verb RunAs -WindowStyle Hidden -Wait`,
    120000,
  );
};

const getWxWorkRunningInfo = async (processes = null) => {
  const wxProcesses = processes || (await findProcesses("WXWork.exe"));
  const versionByPath = new Map();
  const running = [];

  for (const processInfo of wxProcesses) {
    const exePath = processInfo.ExecutablePath || "";
    const key = normalize(exePath);
    if (!versionByPath.has(key)) {
      versionByPath.set(key, await getFileVersion(exePath));
    }
    running.push({
      pid: processInfo.ProcessId,
      path: exePath,
      version: versionByPath.get(key),
    });
  }

  const runningVersions = [
    ...new Set(running.map((processInfo) => processInfo.version).filter(Boolean)),
  ];
  return { running, runningVersions };
};

const getWxWorkInfo = async (config, processes = null) => {
  const configuredPath = String(config?.wxworkExe || "");
  const configuredExists = fileExists(configuredPath);
  const configuredVersion = await getFileVersion(configuredPath);
  const installRoots = await getWxWorkInstallInfo();
  const upgrade = await getWxWorkUpgradeInfo();
  const updaterService = await getWxWorkServiceInfo();
  const runtime = await getWxWorkRunningInfo(processes);
  const configuredPathIsRoot = installRoots.some(
    (rootInfo) => normalize(rootInfo.rootExe) === normalize(configuredPath),
  );
  const configuredCompatible = configuredExists && isTestedWxWorkVersion(configuredVersion);
  const runningCompatible =
    runtime.running.length === 0
      ? null
      : runtime.running.every((processInfo) => isTestedWxWorkVersion(processInfo.version));

  let warning = "";
  if (!configuredPath) {
    warning = "未配置企业微信路径";
  } else if (!configuredExists) {
    warning = "配置的企业微信路径不存在";
  } else if (!configuredCompatible) {
    warning = `当前配置版本 ${configuredVersion || "未知"}，DLL hook 当前按 ${WXWORK_TESTED_VERSION} 验证`;
  } else if (configuredPathIsRoot) {
    warning = "当前使用根目录 WXWork.exe，企业微信更新后容易被替换";
  } else if (updaterService.exists && updaterService.startMode !== "Disabled") {
    warning = "企业微信更新服务仍可用，版本可能再次漂移";
  } else if (upgrade.exists && (upgrade.cachedVersions.length > 0 || upgrade.rootExeExists)) {
    warning = "发现企业微信升级缓存，建议冻结更新";
  } else if (runningCompatible === false) {
    warning = "正在运行的企业微信版本与测试版本不一致";
  }

  const recommendedPath =
    installRoots
      .flatMap((rootInfo) => rootInfo.versionDirs)
      .find((entry) => entry.hasExe && isTestedWxWorkVersion(entry.fileVersion))?.path || "";

  return {
    testedVersion: WXWORK_TESTED_VERSION,
    configured: {
      path: configuredPath,
      exists: configuredExists,
      version: configuredVersion,
      compatible: configuredCompatible,
      rootPath: configuredPathIsRoot,
    },
    running: runtime.running,
    runningVersions: runtime.runningVersions,
    runningCompatible,
    updaterService,
    upgrade,
    installRoots,
    recommendedPath,
    warning,
  };
};

const assertWxWorkReadyForInjection = (info) => {
  if (!info.configured.path) {
    throw new Error("未配置企业微信路径。");
  }
  if (!info.configured.exists) {
    throw new Error(`企业微信路径不存在：${info.configured.path}`);
  }
  if (!info.configured.compatible) {
    throw new Error(
      `企业微信版本不匹配：当前 ${info.configured.version || "未知"}，当前 DLL hook 按 ${info.testedVersion} 验证。请先固定旧版路径。`,
    );
  }
  if (info.runningCompatible === false) {
    throw new Error(
      `检测到正在运行的企业微信版本不是 ${info.testedVersion}，请先关闭新版企业微信再启动插件。`,
    );
  }
};

const findHermesCommand = async () => {
  try {
    const { stdout } = await execPowerShell(
      "$cmd = Get-Command hermes -ErrorAction Stop; $cmd.Source",
      10000,
    );
    const commandPath = stdout.trim();
    return commandPath || "hermes";
  } catch {
    return null;
  }
};

const isProcessAt = async (name, exePath) => {
  const expected = normalize(exePath);
  const processes = await findProcesses(name);
  return processes.some((process) => normalize(process.ExecutablePath) === expected);
};

const waitForPort = async (port, timeoutMs) => {
  const startedAt = Date.now();
  while (Date.now() - startedAt < timeoutMs) {
    if (await isPortOpen(port)) {
      return true;
    }
    await new Promise((resolve) => setTimeout(resolve, 300));
  }
  return false;
};

const waitForProcessAt = async (name, exePath, timeoutMs) => {
  const startedAt = Date.now();
  while (Date.now() - startedAt < timeoutMs) {
    if (await isProcessAt(name, exePath)) {
      return true;
    }
    await new Promise((resolve) => setTimeout(resolve, 300));
  }
  return false;
};

const waitForProcessStopped = async (name, exePath, timeoutMs) => {
  const startedAt = Date.now();
  while (Date.now() - startedAt < timeoutMs) {
    if (!(await isProcessAt(name, exePath))) {
      return true;
    }
    await new Promise((resolve) => setTimeout(resolve, 300));
  }
  return false;
};

const cliConnected = () =>
  cliRuntime.connectedAt > 0 && cliRuntime.connectedAt >= cliRuntime.disconnectedAt;

const cliInjectionLooksHealthy = (adapterHealth = null) => {
  if (!cliConnected()) {
    return false;
  }
  if (cliRuntime.injectAt > 0 && !(cliRuntime.injectResult > 0)) {
    return false;
  }
  if (adapterHealth?.last_poll_at) {
    return true;
  }
  return Date.now() - cliRuntime.connectedAt < 15000;
};

const waitForCliHealthy = async (timeoutMs) => {
  const startedAt = Date.now();
  while (Date.now() - startedAt < timeoutMs) {
    pollCliChatLog();
    const health = await getAdapterHealth(1200);
    if (cliInjectionLooksHealthy(health)) {
      return true;
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  return false;
};

const pidListeningOnPort = (port) =>
  new Promise((resolve) => {
    execFile(
      "powershell.exe",
      [
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        `Get-NetTCPConnection -LocalPort ${port} -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty OwningProcess`,
      ],
      { windowsHide: true, timeout: 4000 },
      (_error, stdout) => {
        const pid = Number(stdout.trim());
        resolve(Number.isFinite(pid) && pid > 0 ? pid : null);
      },
    );
  });

const getProcessInfoByPid = (pid) =>
  new Promise((resolve) => {
    execFile(
      "powershell.exe",
      [
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        `Get-CimInstance Win32_Process -Filter "ProcessId = ${Number(pid) || 0}" | Select-Object ProcessId,Name,ExecutablePath,CommandLine | ConvertTo-Json -Compress`,
      ],
      { windowsHide: true, timeout: 5000 },
      (_error, stdout) => {
        const text = stdout.trim();
        if (!text) {
          resolve(null);
          return;
        }
        try {
          resolve(JSON.parse(text));
        } catch {
          resolve(null);
        }
      },
    );
  });

const listHermesGatewayProcesses = () =>
  new Promise((resolve) => {
    execFile(
      "powershell.exe",
      [
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'hermes.exe' -or $_.CommandLine -like '*hermes*gateway run --accept-hooks*' } | Select-Object ProcessId,Name,ExecutablePath,CommandLine | ConvertTo-Json -Compress",
      ],
      { windowsHide: true, timeout: 6000, maxBuffer: 1024 * 1024 * 4 },
      (_error, stdout) => {
        const text = String(stdout || "").trim();
        if (!text) {
          resolve([]);
          return;
        }
        try {
          const parsed = JSON.parse(text);
          const items = Array.isArray(parsed) ? parsed : [parsed];
          resolve(items.filter((item) => looksLikeHermesProcess(item)));
        } catch {
          resolve([]);
        }
      },
    );
  });

const looksLikeHermesProcess = (processInfo) => {
  if (!processInfo) {
    return false;
  }
  const name = String(processInfo.Name || "").toLowerCase();
  const commandLine = String(processInfo.CommandLine || "").toLowerCase();
  const executablePath = String(processInfo.ExecutablePath || "").toLowerCase();
  return (
    name === "hermes.exe" ||
    executablePath.endsWith("\\hermes.exe") ||
    commandLine.includes("hermes gateway")
  );
};

const healthCheckHermes = async (config, timeoutMs = 3000) => {
  const endpoint = getHermesEndpoint(config);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(endpoint.modelsUrl, {
      headers: config.hermesApiKey
        ? { Authorization: `Bearer ${config.hermesApiKey}` }
        : {},
      signal: controller.signal,
    });
    return {
      ok: response.ok,
      status: response.status,
      endpoint,
    };
  } catch (error) {
    return {
      ok: false,
      status: 0,
      endpoint,
      error,
    };
  } finally {
    clearTimeout(timer);
  }
};

const getAdapterHealth = async (timeoutMs = 2500) => {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(`http://127.0.0.1:${ADAPTER_PORT}/health`, {
      signal: controller.signal,
    });
    if (!response.ok) {
      return null;
    }
    return await response.json();
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
};

const updateAdapterReplyPolicy = async (config, payload, timeoutMs = 3000) => {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(
      `http://127.0.0.1:${ADAPTER_PORT}/control/${encodeURIComponent(
        config.botHookToken || DEFAULT_BOT_HOOK_TOKEN,
      )}`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
        signal: controller.signal,
      },
    );
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    return await response.json();
  } finally {
    clearTimeout(timer);
  }
};

const applyPolicyConfig = async (payload = {}) => {
  const config = saveConfig(payload || {});
  const command = await findHermesCommand().catch(() => null);
  if (command) {
    await configureHermesAdapter(config, command);
  }

  let runtime = null;
  let applied = false;
  if (await isAdapterReady()) {
    runtime = await updateAdapterReplyPolicy(config, {
      reply_mode: config.replyMode,
      wake_words: config.wakeWords,
      allow_all_users: config.allowAllUsers,
    });
    applied = true;
  }

  emitLog(
    `策略已保存：免审批${config.allowAllUsers ? "开启" : "关闭"}，禁用工具集 ${config.disabledToolsets || "无"}`,
  );
  return {
    applied,
    config,
    runtime,
  };
};

const startDetached = (exePath, name, env = {}, args = []) => {
  if (!fileExists(exePath)) {
    throw new Error(`${name} not found: ${exePath}`);
  }

  const logDir = runLogsDir();
  const out = fs.openSync(path.join(logDir, `${name}-${stamp()}.out.log`), "a");
  const err = fs.openSync(path.join(logDir, `${name}-${stamp()}.err.log`), "a");
  const child = spawn(exePath, args, {
    cwd: path.dirname(exePath) || rootDir,
    detached: false,
    windowsHide: true,
    stdio: ["ignore", out, err],
    env: {
      ...process.env,
      ...env,
    },
  });
  child.on("exit", (code) => emitLog(`${name} exited with code ${code ?? "unknown"}.`));
  emitLog(`${name} started.`);
  return child;
};

const runHermesCommand = (hermesCommand, args, config) => {
  const logDir = runLogsDir();
  const out = fs.openSync(path.join(logDir, `hermes-${stamp()}.out.log`), "a");
  const err = fs.openSync(path.join(logDir, `hermes-${stamp()}.err.log`), "a");
  const isCmd = /\.cmd$/i.test(hermesCommand);
  const child = spawn(hermesCommand, args, {
    cwd: rootDir,
    windowsHide: true,
    stdio: ["ignore", out, err],
    shell: isCmd,
    env: {
      ...process.env,
      API_SERVER_ENABLED: "true",
      API_SERVER_KEY: config.hermesApiKey || DEFAULT_HERMES_API_KEY,
      API_SERVER_HOST: getHermesEndpoint(config).host,
      API_SERVER_PORT: String(getHermesEndpoint(config).port),
      HERMES_ACCEPT_HOOKS: "1",
      WECOM_PC_HOOK_ALLOW_ALL_USERS: config.allowAllUsers !== false ? "true" : "false",
      HERMES_DISABLED_TOOLSETS: config.disabledToolsets || "",
    },
  });
  child.on("error", (error) => {
    emitLog(`Hermes command failed: ${error.message || error}`);
  });
  child.on("exit", (code) => {
    if (hermesProcess === child) {
      hermesProcess = null;
    }
    emitLog(`Hermes exited with code ${code ?? "unknown"}.`);
  });
  return child;
};

const configureHermesApiServer = async (config) => {
  const command = await findHermesCommand();
  if (!command) {
    throw new Error("没有找到 hermes 命令。请先安装 Hermes，并确认命令行能执行 hermes。");
  }

  const endpoint = getHermesEndpoint(config);
  if (!isLocalHermesEndpoint(endpoint)) {
    return command;
  }

  const apiKey = config.hermesApiKey || DEFAULT_HERMES_API_KEY;
  const host = endpoint.host === "localhost" ? "127.0.0.1" : endpoint.host;
  const configSets = [
    ["API_SERVER_ENABLED", "true"],
    ["API_SERVER_HOST", host],
    ["API_SERVER_PORT", String(endpoint.port)],
    ["API_SERVER_KEY", apiKey],
  ];

  for (const [key, value] of configSets) {
    await execPowerShell(
      `& '${escapePowerShell(command)}' config set ${key} '${escapePowerShell(value)}'`,
      20000,
    );
  }
  emitLog(`Hermes API Server 已对齐到 ${host}:${endpoint.port}。`);
  return command;
};

const stopByNameAndPath = async (name, exePath) => {
  const expected = normalize(exePath);
  const processes = await findProcesses(name);
  let count = 0;

  for (const processInfo of processes) {
    if (normalize(processInfo.ExecutablePath) !== expected) {
      continue;
    }
    await new Promise((resolve) => {
      execFile(
        "taskkill.exe",
        ["/PID", String(processInfo.ProcessId), "/F", "/T"],
        { windowsHide: true },
        () => resolve(),
      );
    });
    count += 1;
  }

  return count;
};

const stopPidTree = (pid) =>
  new Promise((resolve) => {
    execFile("taskkill.exe", ["/PID", String(pid), "/F", "/T"], { windowsHide: true }, () =>
      resolve(),
    );
  });

const getStatus = async () => {
  const config = loadConfig();
  const wxProcesses = await findProcesses("WXWork.exe");
  const wxworkInfo = await getWxWorkInfo(config, wxProcesses);
  const adapterHealth = await getAdapterHealth();
  const hermesReachable = await canReachHermes(config);
  const adapterReady = await isAdapterReady();
  pollCliChatLog();
  const cliProcessRunning = await isProcessAt("cli.exe", cliExe);
  const cliHealthy = cliProcessRunning && cliInjectionLooksHealthy(adapterHealth);
  return {
    config,
    configPath: getConfigPath(),
    hermes: hermesReachable,
    hermesReady: hermesReachable && adapterReady,
    hermesNeedsRepair: hermesReachable && !adapterReady,
    adapterReady,
    cli: cliProcessRunning,
    cliHealthy,
    wxwork: wxProcesses.some((process) =>
      normalize(process.ExecutablePath).endsWith("\\wxwork.exe"),
    ),
    wxworkInfo,
    files: {
      adapter: fileExists(getHermesPluginSourceDir()),
      cli: fileExists(cliExe),
      wxwork: fileExists(config.wxworkExe),
    },
    desired: {
      plugin: pluginDesiredRunning,
    },
    rootDir,
    adapter: {
      port: ADAPTER_PORT,
      hookUrl: adapterHookUrl(config),
      pluginDir: hermesUserPluginDir(),
      health: adapterHealth,
    },
    cliRuntime: {
      ...cliRuntime,
      connected: cliConnected(),
      healthy: cliHealthy,
    },
    policy: {
      allowAllUsers: config.allowAllUsers !== false,
      runtimeAllowAllUsers: adapterHealth?.allow_all_users ?? null,
      toolPolicyMode: config.toolPolicyMode || "native",
      disabledToolsets: config.disabledToolsets || "",
      soulPath: hermesSoulPath(),
    },
  };
};

const getModuleIcons = async () => ({
  cli: await getFileIconDataUrl(cliExe),
});

const canReachHermes = async (config) => {
  const health = await healthCheckHermes(config, 3000);
  return health.ok;
};

const isAdapterReady = async () => isPortOpen(ADAPTER_PORT);

const shouldAutoStartHermes = (config) => {
  try {
    return isLocalHermesEndpoint(getHermesEndpoint(config));
  } catch {
    return true;
  }
};

const startHermes = async (config = loadConfig(), options = {}) => {
  const endpoint = getHermesEndpoint(config);
  if (!isLocalHermesEndpoint(endpoint)) {
    throw new Error(`当前 Hermes 地址不是本机地址，无法本地启动：${endpoint.baseUrl}`);
  }

  const hermesCommand = await configureHermesApiServer(config);
  await ensureHermesAdapter(config, hermesCommand);

  const initialHealth = await healthCheckHermes(config, 3000);
  const initialAdapterReady = await isAdapterReady();
  if (initialHealth.ok && !options.forceRestart && initialAdapterReady) {
    emitLog("Hermes API is already running.");
    return getStatus();
  }
  if (initialHealth.ok && !options.forceRestart) {
    emitLog("Hermes API is running, but WeCom PC Hook adapter is not loaded. Restarting Hermes gateway to repair it.");
  }

  if (isLocalHermesEndpoint(endpoint)) {
    const existingPid = await pidListeningOnPort(endpoint.port);
    if (existingPid) {
      const info = await getProcessInfoByPid(existingPid);
      if (!looksLikeHermesProcess(info)) {
        throw new Error(
          `端口 ${endpoint.port} 已被 ${info?.Name || "其他进程"}(PID ${existingPid}) 占用，Hermes API 无法启动。`,
        );
      }
      emitLog(`端口 ${endpoint.port} 已有 Hermes 进程，先重启它以应用配置。`);
      await stopPidTree(existingPid);
      await new Promise((resolve) => setTimeout(resolve, 1500));
    }
  }
  hermesStartedByPlugin = true;
  emitLog("Starting Hermes gateway...");
  hermesProcess = runHermesCommand(hermesCommand, ["gateway", "run", "--accept-hooks"], config);

  const startedAt = Date.now();
  while (Date.now() - startedAt < 45000) {
    const health = await healthCheckHermes(config, 3000);
    const adapterReady = await isAdapterReady();
    if (health.ok && adapterReady) {
      emitLog(`Hermes API is ready on ${endpoint.baseUrl}.`);
      return getStatus();
    }
    if (health.status === 401 || health.status === 403) {
      emitLog("Hermes API 已启动，但 API Key 与 GUI 配置不一致。");
      return getStatus();
    }
    await new Promise((resolve) => setTimeout(resolve, 700));
  }

  emitLog(`Hermes API did not become ready within 45 seconds: ${endpoint.baseUrl}.`);
  return getStatus();
};

const stopHermes = async () => {
  const config = loadConfig();
  const endpoint = getHermesEndpoint(config);
  if (!isLocalHermesEndpoint(endpoint)) {
    emitLog(`当前 Hermes 地址不是本机地址，跳过本地停止：${endpoint.baseUrl}`);
    return getStatus();
  }
  const stopTargets = new Set();
  if (hermesProcess?.pid) {
    stopTargets.add(hermesProcess.pid);
  }
  if (isLocalHermesEndpoint(endpoint)) {
    const portPid = await pidListeningOnPort(endpoint.port);
    if (portPid) {
      const info = await getProcessInfoByPid(portPid);
      if (looksLikeHermesProcess(info)) {
        stopTargets.add(portPid);
      } else {
        emitLog(`端口 ${endpoint.port} 由 ${info?.Name || "其他进程"} 占用，未停止。`);
      }
    }
  }
  for (const processInfo of await listHermesGatewayProcesses()) {
    if (processInfo?.ProcessId) {
      stopTargets.add(Number(processInfo.ProcessId));
    }
  }
  let stopped = 0;
  for (const pid of stopTargets) {
    await stopPidTree(pid);
    stopped += 1;
  }
  hermesProcess = null;
  hermesStartedByPlugin = false;
  emitLog(stopped ? `Stopped ${stopped} Hermes process(es).` : "Hermes is not running.");
  return getStatus();
};

const startPlugin = async () => {
  pluginDesiredRunning = true;
  const config = loadConfig();
  assertWxWorkReadyForInjection(await getWxWorkInfo(config));
  await ensureHermesAdapter(config);
  if (!(await canReachHermes(config))) {
    if (shouldAutoStartHermes(config)) {
      await startHermes(config);
    } else {
      emitLog("Hermes API is not reachable. Start Hermes separately when you need AI replies.");
    }
  } else if (!(await isAdapterReady()) && shouldAutoStartHermes(config)) {
    emitLog("Hermes 已运行，但 WeCom PC Hook adapter 未监听，正在重启 Hermes gateway 载入 adapter。");
    await startHermes(config, { forceRestart: true });
  }
  if (!(await getStatus()).wxwork) {
    emitLog("Enterprise WeChat is not running. Open and log in separately before injection.");
  }

  if (await isAdapterReady()) {
    emitLog(`Gateway adapter is ready on ${adapterHookUrl(config)}.`);
  } else {
    emitLog(`Gateway adapter did not open port ${ADAPTER_PORT}. 请检查 Hermes gateway 日志。`);
  }

  const adapterHealth = await getAdapterHealth();
  const cliRunning = await isProcessAt("cli.exe", cliExe);
  if (cliRunning && !cliInjectionLooksHealthy(adapterHealth)) {
    emitLog("CLI injector process is running but hook is not confirmed; restarting CLI injector.");
    await stopByNameAndPath("cli.exe", cliExe);
    await waitForProcessStopped("cli.exe", cliExe, 5000);
  }

  if (!(await isProcessAt("cli.exe", cliExe))) {
    cliProcess = startDetached(cliExe, "cli", {}, [loaderDll, helperDll, config.wxworkExe]);
    if ((await waitForProcessAt("cli.exe", cliExe, 10000)) && (await waitForCliHealthy(12000))) {
      emitLog("CLI injector is connected to WeCom hook.");
    } else {
      emitLog("CLI injector started, but hook is not confirmed yet. If WeCom is logged in, restart WeCom and start the plugin again.");
    }
  } else {
    emitLog(
      cliInjectionLooksHealthy(await getAdapterHealth())
        ? "CLI injector is already connected to WeCom hook."
        : "CLI injector is running, but hook is not confirmed yet.",
    );
  }

  return getStatus();
};

const startWxWork = async () => {
  const config = loadConfig();
  const wxworkInfo = await getWxWorkInfo(config);
  if (wxworkInfo.configured.exists && !wxworkInfo.configured.compatible) {
    emitLog(
      `企业微信路径版本为 ${wxworkInfo.configured.version || "未知"}，当前 hook 按 ${WXWORK_TESTED_VERSION} 验证。`,
    );
  }
  if (await isProcessAt("WXWork.exe", config.wxworkExe)) {
    emitLog("Enterprise WeChat is already running.");
    return getStatus();
  }
  startDetached(config.wxworkExe, "wxwork");
  emitLog("Enterprise WeChat started.");
  return getStatus();
};

const freezeWxWorkUpdates = async () => {
  const steps = [];
  if (!(await isCurrentProcessElevated())) {
    emitLog("当前不是管理员权限，正在请求 UAC 提权来禁用企业微信更新服务。");
    try {
      await runElevatedFreezeWxWorkUpdates();
      elevatedCache = null;
      emitLog("提权冻结命令已执行。");
      return getStatus();
    } catch (error) {
      emitLog(`提权冻结失败或已取消：${error.message || error}`);
      throw new Error("需要以管理员权限禁用 WXWorkUpgrader 服务。");
    }
  }

  const service = await getWxWorkServiceInfo();
  if (service.exists) {
    try {
      await execPowerShell(
        "Stop-Service -Name WXWorkUpgrader -Force -ErrorAction SilentlyContinue; Set-Service -Name WXWorkUpgrader -StartupType Disabled",
        12000,
      );
      steps.push("已禁用 WXWorkUpgrader 服务");
    } catch (error) {
      steps.push(`禁用 WXWorkUpgrader 服务失败：${error.message || error}`);
    }
  } else {
    steps.push("未发现 WXWorkUpgrader 服务");
  }

  const upgradeRoot = wxworkUpgradeRoot();
  if (fileExists(upgradeRoot)) {
    const target = path.join(
      path.dirname(upgradeRoot),
      `upgrade.disabled-${new Date().toISOString().replace(/[-:.TZ]/g, "").slice(0, 14)}`,
    );
    try {
      fs.renameSync(upgradeRoot, target);
      steps.push(`已隔离升级缓存：${target}`);
    } catch (error) {
      steps.push(`隔离升级缓存失败：${error.message || error}`);
    }
  } else {
    steps.push("未发现升级缓存目录");
  }

  for (const root of WXWORK_INSTALL_ROOTS) {
    const updaterDir = path.join(root, "WXWorkUpgrader");
    if (!fileExists(updaterDir)) {
      continue;
    }
    const target = `${updaterDir}.disabled-${new Date()
      .toISOString()
      .replace(/[-:.TZ]/g, "")
      .slice(0, 14)}`;
    try {
      fs.renameSync(updaterDir, target);
      steps.push(`已隔离安装目录更新器：${target}`);
    } catch (error) {
      steps.push(`隔离安装目录更新器失败：${error.message || error}`);
    }
  }

  for (const message of steps) {
    emitLog(message);
  }

  return getStatus();
};

const stopWxWork = async () => {
  const config = loadConfig();
  const stopped = await stopByNameAndPath("WXWork.exe", config.wxworkExe);
  emitLog(stopped ? `Stopped ${stopped} Enterprise WeChat process(es).` : "Enterprise WeChat is not running from configured path.");
  return getStatus();
};

const stopPlugin = async () => {
  pluginDesiredRunning = false;
  const stoppedCli = await stopByNameAndPath("cli.exe", cliExe);
  emitLog(stoppedCli ? `Stopped ${stoppedCli} CLI process(es).` : "CLI injector is not running.");
  await waitForProcessStopped("cli.exe", cliExe, 5000);

  cliProcess = null;
  return getStatus();
};

const createWindow = () => {
  mainWindow = new BrowserWindow({
    width: 980,
    height: 680,
    minWidth: 860,
    minHeight: 580,
    backgroundColor: "#101418",
    title: "Hermes WeCom Plugin",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  mainWindow.removeMenu();
  mainWindow.webContents.once("did-finish-load", startCliChatLogTail);
  mainWindow.loadFile(path.join(__dirname, "renderer.html"));
};

app.whenReady().then(() => {
  ipcMain.handle("status", ipcSafe("status", getStatus));
  ipcMain.handle("module-icons", ipcSafe("module-icons", getModuleIcons));
  ipcMain.handle("save-config", ipcSafe("save-config", (_event, config) => saveConfig(config)));
  ipcMain.handle(
    "apply-reply-policy",
    ipcSafe("apply-reply-policy", async (_event, payload) => {
      const result = await applyPolicyConfig(payload || {});
      emitLog(`回复策略已即时生效：${result.config.replyMode}`);
      return result;
    }),
  );
  ipcMain.handle("apply-policy", ipcSafe("apply-policy", (_event, payload) => applyPolicyConfig(payload || {})));
  ipcMain.handle(
    "open-soul",
    ipcSafe("open-soul", async () => {
      const file = ensureHermesSoulFile();
      await openTextEditor(file);
      emitLog(`已打开人设文件：${file}`);
      return file;
    }),
  );
  ipcMain.handle("browse-exe", async () => {
    const result = await dialog.showOpenDialog(mainWindow, {
      title: "选择可执行文件",
      filters: [{ name: "Executable", extensions: ["exe"] }],
      properties: ["openFile"],
    });
    return result.canceled ? null : result.filePaths[0];
  });
  ipcMain.handle("start-hermes", ipcSafe("start-hermes", () => startHermes()));
  ipcMain.handle("stop-hermes", ipcSafe("stop-hermes", stopHermes));
  ipcMain.handle("start-wxwork", ipcSafe("start-wxwork", startWxWork));
  ipcMain.handle("stop-wxwork", ipcSafe("stop-wxwork", stopWxWork));
  ipcMain.handle("freeze-wxwork-updates", ipcSafe("freeze-wxwork-updates", freezeWxWorkUpdates));
  ipcMain.handle("start-plugin", ipcSafe("start-plugin", startPlugin));
  ipcMain.handle("stop-plugin", ipcSafe("stop-plugin", stopPlugin));
  ipcMain.handle("open-folder", async () => shell.openPath(rootDir));
  ipcMain.handle("open-logs", async () => shell.openPath(runLogsDir()));

  createWindow();
});

app.on("window-all-closed", () => {
  if (cliLogTailTimer) {
    clearInterval(cliLogTailTimer);
    cliLogTailTimer = null;
  }
  app.quit();
});
