# 部署到 Streamlit Community Cloud（免费固定链接）

部署完成后你会拿到一个 `https://<你的应用名>.streamlit.app` 的固定地址，别人浏览器打开即可使用，
**不需要你的电脑开机**。

---

## 一、这个目录已经准备好的东西

| 项 | 说明 |
|---|---|
| `app.py` | 主入口（与提交版一致） |
| `requirements.txt` | 已修正：`httpx2`（不存在的包）→ `httpx`；`streamlit>=1.65` |
| `src/` | 全部业务代码 |
| `outputs/researchpilot.db` | **演示数据库**（2 个项目、27 篇文献、90 条证据），部署后直接有内容 |
| `outputs/uploads/` | 两篇论文的 PDF（Bensen 2007、Xu 2025），保证阅读页能打开 |
| `.streamlit/config.toml` | 关闭首次运行邮箱询问、关闭遥测 |
| `.streamlit/secrets.toml.example` | Secrets 模板（**真正的 secrets.toml 不进仓库**） |
| `src/researchpilot/config.py` | 加了 `st.secrets → 环境变量` 桥接，云端能读到模型 Key |

已在本机用「无 .env + 相对路径」的等价条件跑过一遍：能启动、能读到 2 个项目、AI 状态正确，无报错。

---

## 二、推到 GitHub（你来做，需要你的账号）

1. 浏览器打开 https://github.com/new ，仓库名填 `ResearchPilot`，**Public**（免费账号用公开仓库最省事），
   不要勾选 "Add a README"。
2. 在本目录执行（把 `<你的用户名>` 换成你的）：

```powershell
cd C:\Users\12345\Documents\deepseek-harness\default-workspace\cloud_deploy\ResearchPilot
git remote add origin https://github.com/<你的用户名>/ResearchPilot.git
git push -u origin main
```

推送时会弹窗要 GitHub 账号授权（浏览器登录即可）。仓库约 13 MB。

---

## 三、在 Streamlit Cloud 建应用

1. 打开 https://share.streamlit.io ，用 **GitHub 账号**登录。
2. 点 **Create app** → **Deploy a public app from GitHub**。
3. 填三项：
   - Repository：`<你的用户名>/ResearchPilot`
   - Branch：`main`
   - Main file path：`app.py`
4. 点 **Advanced settings**：
   - **Python version** 选 **3.11**
   - **Secrets** 粘贴下面四行（把 Key 换成你自己的）：

```toml
LLM_PROVIDER = "deepseek"
LLM_API_KEY = "sk-你的Key"
LLM_MODEL = "deepseek-flash"
LLM_BASE_URL = "https://api.deepseek.com"
```

5. 点 **Deploy**，等 2–5 分钟（首次装依赖）。

---

## 四、验收清单

拿到链接后自己先过一遍：

- [ ] 首页左侧「所有项目（2）」能看到地震与遥感两个项目
- [ ] 点开 Bensen 那篇，右侧「概览」有四段式解读与页码角标
- [ ] 「引用」标签里有 19 条待裁决关系
- [ ] 左下角显示「AI 模型 · 已连接」（说明 Secrets 生效）
- [ ] 右下角 AI 浮窗能提问并回答
- [ ] 打开 PDF 原文能翻页（说明 uploads 随仓库带上了）

---

## 五、必须知道的三个限制

1. **数据库是临时的**：云端实例重启/休眠唤醒后会回到仓库里的那份 `researchpilot.db`，
   期间的改动会丢。演示用没问题，别当生产库。
2. **会休眠**：免费实例无人访问约 15 分钟后休眠，**评委第一次打开可能要等 20–40 秒**。
   建议路演前 5 分钟自己先打开一次「预热」。
3. **内存 1 GB**：本项目占用很小（无 obspy 计算引擎），正常够用。

---

## 六、如果云端报错

| 现象 | 原因 | 处理 |
|---|---|---|
| 部署失败，日志停在 `pip install` | 依赖写错 | 确认 `requirements.txt` 里是 `httpx`，不是 `httpx2` |
| 页面报 `No module named 'researchpilot'` | 没找到 `src` | 确认仓库根目录有 `app.py`，且 `src/` 完整 |
| 「AI 模型 · 未连接」 | Secrets 没填或名字写错 | 回到 Settings → Secrets，逐行核对四项 |
| 文献库空白 | 数据库没进仓库 | `git ls-files outputs` 应能看到 `researchpilot.db` |
| PDF 打不开 | uploads 没进仓库 | `git ls-files outputs/uploads` 应有对应 PDF |
