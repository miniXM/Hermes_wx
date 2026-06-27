const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("pluginApi", {
  getStatus: () => ipcRenderer.invoke("status"),
  getModuleIcons: () => ipcRenderer.invoke("module-icons"),
  saveConfig: (config) => ipcRenderer.invoke("save-config", config),
  applyReplyPolicy: (config) => ipcRenderer.invoke("apply-reply-policy", config),
  applyPolicy: (config) => ipcRenderer.invoke("apply-policy", config),
  openSoul: () => ipcRenderer.invoke("open-soul"),
  browseExe: () => ipcRenderer.invoke("browse-exe"),
  startHermes: () => ipcRenderer.invoke("start-hermes"),
  stopHermes: () => ipcRenderer.invoke("stop-hermes"),
  startWxWork: () => ipcRenderer.invoke("start-wxwork"),
  stopWxWork: () => ipcRenderer.invoke("stop-wxwork"),
  freezeWxWorkUpdates: () => ipcRenderer.invoke("freeze-wxwork-updates"),
  startPlugin: () => ipcRenderer.invoke("start-plugin"),
  stopPlugin: () => ipcRenderer.invoke("stop-plugin"),
  openFolder: () => ipcRenderer.invoke("open-folder"),
  openLogs: () => ipcRenderer.invoke("open-logs"),
  onLog: (callback) => {
    const listener = (_event, entry) => callback(entry);
    ipcRenderer.on("log", listener);
    return () => ipcRenderer.removeListener("log", listener);
  },
});
