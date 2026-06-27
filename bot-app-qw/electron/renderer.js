const cards = {
  hermes: document.querySelector('[data-key="hermes"]'),
  adapter: document.querySelector('[data-key="adapter"]'),
  cli: document.querySelector('[data-key="cli"]'),
  wxwork: document.querySelector('[data-key="wxwork"]'),
};

const labels = {
  hermes: document.getElementById("hermesText"),
  adapter: document.getElementById("adapterText"),
  cli: document.getElementById("cliText"),
  wxwork: document.getElementById("wxworkText"),
};

const powerToggle = document.getElementById("powerToggle");
const powerText = document.getElementById("powerText");
const logList = document.getElementById("logList");
const adapterPath = document.getElementById("adapterPath");
const cliPath = document.getElementById("cliPath");
const hermesBaseUrl = document.getElementById("hermesBaseUrl");
const wxworkPath = document.getElementById("wxworkPath");
const hermesModuleText = document.getElementById("hermesModuleText");
const wxworkModuleText = document.getElementById("wxworkModuleText");
const policyTabs = document.querySelector(".policy-tabs");
const replyModeButtons = document.querySelectorAll("[data-reply-mode]");
const wakeWords = document.getElementById("wakeWords");
const replyModeHint = document.getElementById("replyModeHint");
const allowAllUsers = document.getElementById("allowAllUsers");
const toolPolicyMode = document.getElementById("toolPolicyMode");
const disabledToolsets = document.getElementById("disabledToolsets");
const savePolicyButton = document.getElementById("savePolicy");
const openSoulButton = document.getElementById("openSoul");
const policyStatus = document.getElementById("policyStatus");
const browseButtons = document.querySelectorAll("[data-browse]");
const moduleIcons = {
  hermes: document.getElementById("hermesIcon"),
  adapter: document.getElementById("adapterIcon"),
  cli: document.getElementById("cliIcon"),
  wxwork: document.getElementById("wxworkIcon"),
};
const hermesToggle = document.getElementById("hermesToggle");
const hermesToggleText = document.getElementById("hermesToggleText");
const wxworkToggle = document.getElementById("wxworkToggle");
const wxworkToggleText = document.getElementById("wxworkToggleText");
const freezeWxwork = document.getElementById("freezeWxwork");

let lastStatus = null;
let pluginDesired = false;
let busy = false;
let currentReplyMode = "all";
let policyBusy = false;
let strategyBusy = false;
let strategyDirty = false;

const chatOnlyDisabledToolsets = [
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
].join(",");

const replyModeLabel = (mode) =>
  ({
    all: "全部回复",
    wake: "唤醒词",
    off: "暂停",
  })[mode] || "未知";

const formatRuntimeTime = (value) => {
  const numeric = Number(value || 0);
  if (!numeric) {
    return "无";
  }
  const ms = numeric > 1000000000000 ? numeric : numeric * 1000;
  return new Date(ms).toLocaleTimeString();
};

const setModuleIcon = (key, src) => {
  const img = moduleIcons[key];
  if (!img || !src) {
    img?.parentElement?.classList.add("missing");
    return;
  }
  img.src = src;
  img.parentElement?.classList.remove("missing");
};

const refreshModuleIcons = async () => {
  if (!window.pluginApi.getModuleIcons) {
    return;
  }
  const icons = await window.pluginApi.getModuleIcons();
  setModuleIcon("hermes", "./assets/hermes.png");
  setModuleIcon("adapter", "./assets/hermes.png");
  setModuleIcon("cli", icons.cli);
  setModuleIcon("wxwork", "./assets/wecom.png");
};

const addLog = (message, at = new Date().toLocaleTimeString()) => {
  const entry = document.createElement("div");
  entry.className = "log-entry";
  const time = document.createElement("time");
  time.textContent = at;
  const text = document.createElement("span");
  text.textContent = message;
  entry.append(time, text);
  logList.append(entry);

  while (logList.childElementCount > 120) {
    logList.firstElementChild?.remove();
  }
  logList.scrollTop = logList.scrollHeight;
};

const setCard = (key, running, runningText, stoppedText) => {
  cards[key].classList.toggle("running", Boolean(running));
  cards[key].classList.toggle("stopped", !running);
  cards[key].classList.toggle("warning", false);
  labels[key].textContent = running ? runningText : stoppedText;
};

const setWarningCard = (key, text) => {
  cards[key].classList.remove("running", "stopped");
  cards[key].classList.add("warning");
  labels[key].textContent = text;
};

const setSwitch = (button, on) => {
  button.classList.toggle("is-on", Boolean(on));
  button.setAttribute("aria-checked", on ? "true" : "false");
};

const setReplyMode = (mode) => {
  currentReplyMode = mode || "all";
  replyModeButtons.forEach((button) => {
    button.classList.toggle("active", button.dataset.replyMode === currentReplyMode);
  });
  wakeWords.disabled = currentReplyMode !== "wake";
  wakeWords.style.opacity = currentReplyMode === "wake" ? "1" : "0.45";
};

const renderReplyModeHint = (status, effectiveReplyMode) => {
  const savedReplyMode = status.config?.replyMode || "all";
  const runtimeReplyMode = status.adapter?.health?.reply_mode || null;
  if (!replyModeHint) {
    return;
  }
  if (!status.adapterReady || !runtimeReplyMode) {
    replyModeHint.textContent = `当前保存：${replyModeLabel(savedReplyMode)}`;
    replyModeHint.classList.remove("pending");
    return;
  }
  if (runtimeReplyMode !== savedReplyMode) {
    replyModeHint.textContent = `当前生效：${replyModeLabel(runtimeReplyMode)}，已保存：${replyModeLabel(savedReplyMode)}，重启插件后生效`;
    replyModeHint.classList.add("pending");
    return;
  }
  replyModeHint.textContent = `当前生效：${replyModeLabel(effectiveReplyMode)}`;
  replyModeHint.classList.remove("pending");
};

const setAllBusy = (nextBusy) => {
  busy = nextBusy;
  powerToggle.classList.toggle("busy", nextBusy);
  hermesToggle.classList.toggle("busy", nextBusy);
  wxworkToggle.classList.toggle("busy", nextBusy);
  freezeWxwork.classList.toggle("busy", nextBusy);
  [powerToggle, hermesToggle, wxworkToggle, freezeWxwork, ...browseButtons].forEach((button) => {
    button.disabled = nextBusy;
  });
  if (savePolicyButton) {
    savePolicyButton.disabled = nextBusy || strategyBusy;
  }
  if (openSoulButton) {
    openSoulButton.disabled = nextBusy || strategyBusy;
  }
  if (allowAllUsers) {
    allowAllUsers.disabled = nextBusy || strategyBusy;
  }
  if (toolPolicyMode) {
    toolPolicyMode.disabled = nextBusy || strategyBusy;
  }
  if (disabledToolsets) {
    disabledToolsets.disabled = nextBusy || strategyBusy;
  }
};

const renderToolPolicyInputs = (mode, savedDisabled = "") => {
  const nextMode = ["native", "chat", "custom"].includes(mode) ? mode : "native";
  if (toolPolicyMode) {
    toolPolicyMode.value = nextMode;
  }
  if (!disabledToolsets) {
    return;
  }
  if (nextMode === "chat") {
    disabledToolsets.value = chatOnlyDisabledToolsets;
    disabledToolsets.disabled = true;
  } else if (nextMode === "native") {
    disabledToolsets.value = "";
    disabledToolsets.disabled = true;
  } else {
    disabledToolsets.disabled = busy || strategyBusy;
    if (document.activeElement !== disabledToolsets) {
      disabledToolsets.value = savedDisabled || "";
    }
  }
};

const renderPluginControl = (status) => {
  if (!busy) {
    pluginDesired = Boolean(status.desired?.plugin || status.cli);
  }
  setSwitch(powerToggle, pluginDesired);
  const pluginRunning = Boolean(status.cli);
  if (pluginDesired && pluginRunning) {
    powerText.textContent = "运行中";
  } else if (pluginDesired) {
    powerText.textContent = "已开启";
  } else {
    powerText.textContent = "已关闭";
  }
};

const renderStatus = (status) => {
  lastStatus = status;
  if (status.hermesReady) {
    setCard("hermes", true, "已就绪", "未启动");
  } else if (status.hermesNeedsRepair) {
    setWarningCard("hermes", "需修复");
  } else {
    setCard("hermes", false, "已就绪", "未启动");
  }
  setCard("adapter", status.adapterReady, "监听中", "未监听");
  if (status.cliHealthy) {
    setCard("cli", true, "已注入", "未启动");
  } else if (status.cli) {
    setWarningCard("cli", "未确认");
  } else {
    setCard("cli", false, "已注入", "未启动");
  }
  setCard("wxwork", status.wxwork, "运行中", "未启动");
  renderPluginControl(status);
  setSwitch(hermesToggle, status.hermes);
  hermesToggleText.textContent = status.hermesNeedsRepair ? "修复" : status.hermes ? "运行中" : "已关闭";
  if (status.hermesNeedsRepair) {
    hermesModuleText.textContent = "Gateway API 运行中，但 WeCom adapter 未加载；点击开关可修复";
    hermesModuleText.classList.add("warning-text");
  } else {
    hermesModuleText.textContent = status.hermesReady
      ? "Gateway API 与 WeCom adapter 已就绪"
      : "Gateway API，插件回复依赖它";
    hermesModuleText.classList.remove("warning-text");
  }
  setSwitch(wxworkToggle, status.wxwork);
  wxworkToggleText.textContent = status.wxwork ? "运行中" : "已关闭";
  const wxInfo = status.wxworkInfo;
  if (wxInfo?.warning) {
    wxworkModuleText.textContent = wxInfo.warning;
    wxworkModuleText.classList.add("warning-text");
  } else {
    wxworkModuleText.textContent = status.wxwork
      ? "企业微信已运行，CLI 可进行注入"
      : "企业微信客户端，需要登录后 CLI 才能注入";
    wxworkModuleText.classList.remove("warning-text");
  }

  const cliRuntime = status.cliRuntime || {};
  const adapterHealth = status.adapter?.health || {};
  const cliModuleText = document.querySelector('[data-module-text="cli"]');
  const adapterModuleText = document.querySelector('[data-module-text="adapter"]');
  if (cliModuleText) {
    if (status.cliHealthy) {
      cliModuleText.textContent = `已连接 hook｜上行 ${formatRuntimeTime(cliRuntime.receivedAt)}｜下发 ${formatRuntimeTime(cliRuntime.outboundAt)}`;
      cliModuleText.classList.remove("warning-text");
    } else if (status.cli) {
      cliModuleText.textContent = cliRuntime.lastError || "进程存在，但还没有确认注入成功；重启插件会自动重启 CLI";
      cliModuleText.classList.add("warning-text");
    } else {
      cliModuleText.textContent = "注入企业微信，轮询 adapter 下行队列";
      cliModuleText.classList.remove("warning-text");
    }
  }
  if (adapterModuleText) {
    adapterModuleText.textContent = status.adapterReady
      ? `监听中｜CLI轮询 ${formatRuntimeTime(adapterHealth.last_poll_at)}｜上行 ${formatRuntimeTime(adapterHealth.last_inbound_at)}`
      : "随 Hermes gateway 运行，接收 CLI hook 后进入 Hermes Agent 会话";
  }

  adapterPath.value = status.adapter?.hookUrl || "http://127.0.0.1:8001/hook/testtoken";
  cliPath.value = `${status.rootDir || ""}\\cli.exe`;
  if (document.activeElement !== hermesBaseUrl) {
    hermesBaseUrl.value = status.config?.hermesBaseUrl || "http://127.0.0.1:8642/v1";
  }
  if (document.activeElement !== wxworkPath) {
    wxworkPath.value = status.config?.wxworkExe || "";
  }

  const replyMode = status.adapter?.health?.reply_mode || status.config?.replyMode || "all";
  if (!policyBusy) {
    setReplyMode(replyMode);
  }
  renderReplyModeHint(status, replyMode);
  if (document.activeElement !== wakeWords) {
    wakeWords.value = status.config?.wakeWords || "@Hermes,Hermes";
  }
  if (!strategyDirty && allowAllUsers) {
    allowAllUsers.checked = status.policy?.allowAllUsers !== false;
  }
  if (!strategyDirty) {
    renderToolPolicyInputs(status.policy?.toolPolicyMode || "native", status.policy?.disabledToolsets || "");
  }
  if (policyStatus) {
    const runtime = status.policy?.runtimeAllowAllUsers;
    const saved = status.policy?.allowAllUsers !== false;
    if (runtime === null || runtime === undefined) {
      policyStatus.textContent = `已保存：${saved ? "免审批开启" : "需要审批"}`;
    } else if (runtime !== saved) {
      policyStatus.textContent = `运行中：${runtime ? "免审批开启" : "需要审批"}，保存值：${saved ? "免审批开启" : "需要审批"}`;
    } else {
      const modeLabel = {
        native: "原生",
        chat: "只聊天",
        custom: "自定义",
      }[status.policy?.toolPolicyMode || "native"];
      policyStatus.textContent = `${saved ? "免审批开启" : "需要审批"}｜工具策略：${modeLabel}`;
    }
  }
};

const refresh = async () => {
  const status = await window.pluginApi.getStatus();
  renderStatus(status);
  return status;
};

const saveConfigFromInputs = async () => {
  const config = await window.pluginApi.saveConfig({
    hermesBaseUrl: hermesBaseUrl.value.trim() || "http://127.0.0.1:8642/v1",
    hermesApiKey: lastStatus?.config?.hermesApiKey || "change-me-local-dev",
    botHookToken: lastStatus?.config?.botHookToken || "testtoken",
    wxworkExe: wxworkPath.value.trim(),
    replyMode: currentReplyMode,
    wakeWords: wakeWords.value.trim() || "@Hermes,Hermes",
  });
  addLog("配置已保存。");
  return config;
};

const saveStrategyPolicy = async () => {
  if (strategyBusy) {
    return;
  }
  strategyBusy = true;
  if (savePolicyButton) {
    savePolicyButton.disabled = true;
  }
  if (openSoulButton) {
    openSoulButton.disabled = true;
  }
  if (allowAllUsers) {
    allowAllUsers.disabled = true;
  }
  if (disabledToolsets) {
    disabledToolsets.disabled = true;
  }
  try {
    const result = await window.pluginApi.applyPolicy({
      hermesBaseUrl: hermesBaseUrl.value.trim() || "http://127.0.0.1:8642/v1",
      hermesApiKey: lastStatus?.config?.hermesApiKey || "change-me-local-dev",
      botHookToken: lastStatus?.config?.botHookToken || "testtoken",
      wxworkExe: wxworkPath.value.trim(),
      replyMode: currentReplyMode,
      wakeWords: wakeWords.value.trim() || "@Hermes,Hermes",
      allowAllUsers: allowAllUsers?.checked !== false,
      toolPolicyMode: toolPolicyMode?.value || "native",
      disabledToolsets: disabledToolsets?.value.trim() || "",
    });
    strategyDirty = false;
    addLog(result.applied ? "策略已保存并同步到运行中 adapter。" : "策略已保存，启动后会生效。");
    await refresh();
  } catch (error) {
    addLog(`策略保存失败：${error.message || error}`);
    await refresh();
  } finally {
    strategyBusy = false;
    if (savePolicyButton) {
      savePolicyButton.disabled = busy;
    }
    if (openSoulButton) {
      openSoulButton.disabled = busy;
    }
    if (allowAllUsers) {
      allowAllUsers.disabled = busy;
    }
    if (toolPolicyMode) {
      toolPolicyMode.disabled = busy;
    }
    if (disabledToolsets) {
      renderToolPolicyInputs(toolPolicyMode?.value || "native", disabledToolsets.value);
    }
  }
};

const runAction = async (label, action, afterStart) => {
  if (busy) {
    return;
  }
  setAllBusy(true);
  try {
    await saveConfigFromInputs();
    const status = await action();
    if (afterStart) {
      afterStart(status);
    } else {
      renderStatus(status);
    }
  } catch (error) {
    addLog(`${label}失败：${error.message || error}`);
    await refresh();
  } finally {
    setAllBusy(false);
  }
};

const saveReplyPolicy = async () => {
  if (policyBusy) {
    return;
  }
  policyBusy = true;
  policyTabs.classList.add("busy");
  replyModeButtons.forEach((button) => {
    button.disabled = true;
  });
  const previousMode = lastStatus?.config?.replyMode || "all";
  try {
    const wakeValue = wakeWords.value.trim() || "@Hermes,Hermes";
    const result = await window.pluginApi.applyReplyPolicy({
      hermesBaseUrl: hermesBaseUrl.value.trim() || "http://127.0.0.1:8642/v1",
      hermesApiKey: lastStatus?.config?.hermesApiKey || "change-me-local-dev",
      botHookToken: lastStatus?.config?.botHookToken || "testtoken",
      wxworkExe: wxworkPath.value.trim(),
      replyMode: currentReplyMode,
      wakeWords: wakeValue,
    });
    addLog(result.applied ? "回复策略已即时生效。" : "回复策略已保存，待插件启动后生效。");
    await refresh();
  } catch (error) {
    setReplyMode(previousMode);
    addLog(`回复策略保存失败：${error.message || error}`);
  } finally {
    policyBusy = false;
    policyTabs.classList.remove("busy");
    replyModeButtons.forEach((button) => {
      button.disabled = false;
    });
  }
};

powerToggle.addEventListener("click", () => {
  const shouldStart = !pluginDesired;
  pluginDesired = shouldStart;
  setSwitch(powerToggle, shouldStart);
  powerText.textContent = shouldStart ? "启动中" : "停止中";
  addLog(shouldStart ? "正在启动插件..." : "正在停止插件...");
  runAction(
    shouldStart ? "启动插件" : "停止插件",
    shouldStart ? window.pluginApi.startPlugin : window.pluginApi.stopPlugin,
  );
});

wxworkToggle.addEventListener("click", () => {
  const shouldStart = !lastStatus?.wxwork;
  setSwitch(wxworkToggle, shouldStart);
  wxworkToggleText.textContent = shouldStart ? "启动中" : "停止中";
  addLog(shouldStart ? "正在打开企业微信..." : "正在停止企业微信...");
  runAction(
    shouldStart ? "打开企业微信" : "停止企业微信",
    shouldStart ? window.pluginApi.startWxWork : window.pluginApi.stopWxWork,
  );
});

hermesToggle.addEventListener("click", () => {
  const shouldStart = !lastStatus?.hermes || lastStatus?.hermesNeedsRepair;
  setSwitch(hermesToggle, shouldStart);
  hermesToggleText.textContent = lastStatus?.hermesNeedsRepair ? "修复中" : shouldStart ? "启动中" : "停止中";
  addLog(lastStatus?.hermesNeedsRepair ? "正在修复Hermes链路..." : shouldStart ? "正在打开Hermes..." : "正在停止Hermes...");
  runAction(
    lastStatus?.hermesNeedsRepair ? "修复Hermes" : shouldStart ? "打开Hermes" : "停止Hermes",
    shouldStart ? window.pluginApi.startHermes : window.pluginApi.stopHermes,
  );
});

freezeWxwork.addEventListener("click", () => {
  addLog("正在冻结企业微信更新链路...");
  runAction("冻结企业微信更新", window.pluginApi.freezeWxWorkUpdates);
});

browseButtons.forEach((button) => {
  button.addEventListener("click", async () => {
    if (busy) {
      return;
    }
    const input = document.getElementById(button.dataset.browse);
    const selected = await window.pluginApi.browseExe();
    if (!selected) {
      return;
    }
    input.value = selected;
    await saveConfigFromInputs();
    await refresh();
  });
});

wxworkPath.addEventListener("change", async () => {
  await saveConfigFromInputs();
  await refresh();
});

hermesBaseUrl.addEventListener("change", async () => {
  await saveConfigFromInputs();
  await refresh();
});

replyModeButtons.forEach((button) => {
  button.addEventListener("click", () => {
    const nextMode = button.dataset.replyMode || "all";
    if (nextMode === currentReplyMode && !policyBusy) {
      return;
    }
    setReplyMode(nextMode);
    saveReplyPolicy();
  });
});

wakeWords.addEventListener("change", saveReplyPolicy);

savePolicyButton?.addEventListener("click", saveStrategyPolicy);

toolPolicyMode?.addEventListener("change", () => {
  strategyDirty = true;
  renderToolPolicyInputs(toolPolicyMode.value, disabledToolsets?.value || "");
  policyStatus.textContent = "策略已修改，待保存";
});

disabledToolsets?.addEventListener("input", () => {
  if (toolPolicyMode?.value === "custom") {
    strategyDirty = true;
    policyStatus.textContent = "策略已修改，待保存";
  }
});

allowAllUsers?.addEventListener("change", () => {
  strategyDirty = true;
  policyStatus.textContent = "策略已修改，待保存";
});

openSoulButton?.addEventListener("click", async () => {
  try {
    const file = await window.pluginApi.openSoul();
    addLog(`已打开人设文件：${file}`);
  } catch (error) {
    addLog(`打开人设文件失败：${error.message || error}`);
  }
});

window.pluginApi.onLog((entry) => addLog(entry.message, entry.at));

refresh()
  .then((status) => {
    addLog("控制台已就绪。");
    if (status.configPath) {
      addLog(`配置文件：${status.configPath}`);
    }
  })
  .catch((error) => addLog(`初始状态检测失败：${error.message || error}`));

refreshModuleIcons().catch(() => undefined);

setInterval(() => {
  if (!busy) {
    refresh().catch(() => undefined);
  }
}, 3000);
