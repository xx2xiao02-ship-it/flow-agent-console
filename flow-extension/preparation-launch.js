// Capture the private launch fragment before Flow's router/project discovery
// can replace it. The sender's Chrome tab ID supplies ownership, not the page.
if (/^#flow-console-page=\d{1,5}\.[a-f0-9]{64}$/.test(location.hash)) {
  chrome.runtime.sendMessage({ flowConsolePreparation: location.href }).catch(() => {});
}
