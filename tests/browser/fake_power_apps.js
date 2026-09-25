/* A stand-in for @microsoft/power-apps (its app and data entry points) in the headless test. The host calls
 * getContext() and getClient(dataSourcesInfo).executeAsync(...) exactly as it does in Power Apps; here the test's
 * player page answers them over postMessage, the way the Power Apps player answers the real SDK. */
let next = 1;
const pending = {};

window.addEventListener("message", (ev) => {
  const d = ev.data;
  if (ev.source !== window.parent || !d || d.type !== "fake-sdk:result" || !pending[d.id]) return;
  const done = pending[d.id];
  delete pending[d.id];
  done(d.result);
});

function ask(kind, payload) {
  const id = "s" + next++;
  return new Promise((resolve) => {
    pending[id] = resolve;
    window.parent.postMessage({ type: "fake-sdk", id, kind, payload }, "*");
  });
}

export function getContext() {
  return ask("context", {});
}

export function getClient(dataSourcesInfo) {
  return {
    executeAsync: (operation) => ask("execute", { operation, dataSources: Object.keys(dataSourcesInfo || {}) }),
  };
}
