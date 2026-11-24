import { useState } from "react";

const repository = "https://github.com/Yinglianchun/Serein";
const api = "https://api.github.com/repos/Yinglianchun/Serein";
const revision = import.meta.env.VITE_SEREIN_COMMIT || "";
const current = /^[0-9a-f]{40}$/.test(revision) ? revision : "";

export function VersionUpdate() {
  const [checking, setChecking] = useState(false);
  const [message, setMessage] = useState("");

  async function check() {
    if (checking) return;
    setChecking(true);
    setMessage("正在查询 GitHub…");
    try {
      const path = current ? `/compare/${current}...main?per_page=1` : "/commits/main";
      const response = await fetch(api + path, {
        credentials: "omit", referrerPolicy: "no-referrer", cache: "no-store",
        signal: AbortSignal.timeout(10000),
      });
      if (current && response.status === 404) {
        setMessage("GitHub 无法比对此构建提交。可打开问题日志，或在安装目录运行 se 核对版本。");
        return;
      }
      if (!response.ok) throw new Error();
      const data = await response.json();
      if (!current && /^[0-9a-f]{40}$/.test(data.sha)) {
        setMessage(`main 最新提交为 ${data.sha.slice(0, 12)}。本次构建未记录提交，无法判断是否需要更新。`);
      } else if (data.status === "identical") {
        setMessage("当前构建提交与 main 一致。");
      } else if (data.status === "ahead") {
        setMessage(`有新版本，main 多了 ${data.ahead_by} 个提交。`);
      } else if (data.status === "behind") {
        setMessage("当前构建已包含 main 的最新提交，另有本地提交。");
      } else if (data.status === "diverged") {
        setMessage("当前构建与 main 各有独立提交，请核对本地改动后再更新。");
      } else throw new Error();
    } catch {
      setMessage("暂时查不到版本，请检查 GitHub 连接或稍后重试。");
    } finally {
      setChecking(false);
    }
  }

  return <section className="settings-group">
    <div className="settings-group__heading">
      <h3>更新版本</h3>
      <p>当前构建提交：{current ? current.slice(0, 12) : "未记录"}</p>
    </div>
    <div className="settings-actions">
      <button type="button" onClick={check} disabled={checking}>{checking ? "检查中…" : "检查更新"}</button>
      <a href={repository + "/blob/main/docs/issue-history.md"} target="_blank" rel="noreferrer">更新与问题日志</a>
    </div>
    <p role="status">{message}</p>
    <p>要安装更新，在实例安装目录运行 <code>se</code>，选择「1 · 拉取上游代码并重建」，按提示备份。</p>
  </section>;
}
