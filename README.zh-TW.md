<h1 align="center">cities-skylines-ai-agent</h1>

<p align="center">
  讓 AI 當市長玩《Cities: Skylines 1》。它透過本機 API 橋接 MOD 讀取城市的數據，規劃道路、
  劃區和公共服務；每次改動都先建立 checkpoint，改完再驗證。
</p>

<p align="center">
  <a href="README.md">English README</a> ·
  <a href="docs/api.md">API 參考</a> ·
  <a href="docs/research/cs1-planning-knowledge-zh-TW.md">CS1 規劃知識</a> ·
  <a href="CONTRIBUTING.md">貢獻指南</a>
</p>

本專案 fork 自
[Sunwood-ai-labs/cities-skylines1-agent-skill](https://github.com/Sunwood-ai-labs/cities-skylines1-agent-skill)
（見文末[致謝](#-致謝)）。原專案做出了把遊戲變成本機 HTTP API 的 CS1 MOD；這個 fork 在它的基礎上，
發展成能自己持續經營、擴張城市的 agent：

- **99 個有型別的市長工具**：每個都有 schema、preview 和安全檢查，涵蓋精確到 block 的劃區、
  臨街道路提案、水電規劃、道路升級、區域政策和拆除影響評估等。
- **每次改動都走同一套流程**：preview → 驗證過的 checkpoint → regression 基準 → 改動 → 讓模擬穩定 →
  針對性檢查 → regression 比對。
- **用遊戲自己的規則驗證**：例如道路升級後，會用遊戲原生的 `Building.CheckZoning` 檢查旁邊的建物，
  避免它們在不知不覺中被拆掉；蓋路則會檢查道路落地後和地形之間的淨空。
- **自主遊玩迴圈**（`scripts/loop/`）：自己維持城市運作，包括公共服務、水電、劃區節奏、
  交通處理（拓寬、路口控制、公車、繞道、高速公路出口、地鐵）和貨運鐵路（鐵路旁沒有空地時，
  在工業區旁蓋貨運站，再用地下隧道接到遠處的鐵路），每個動作事後都依實際變化判斷有沒有效。
  商業區空著的格子會逐格改劃（`set-zone` 的 `vacantCellsOnly`），缺工時優先劃住宅。
  只有迴圈處理不了的結構性問題才需要大模型（例如 Claude）判斷。可選用本機小模型交叉核對讀數（`--model`），
  它不會呼叫任何改動城市的工具。
- **整理過的遊戲知識**：需求節奏、劃區的路口限制、高速公路立體交叉、水電、垃圾掩埋場和道路等級等，
  都寫成精簡的規則（[docs/research](docs/research/cs1-planning-knowledge-zh-TW.md)）。

Bridge 完全不用截圖或影像辨識，城市狀態全部來自遊戲本身的數據。

## 🔒 安全設計

Bridge 能控制正在執行的遊戲，而且沒有登入驗證，所以只服務本機上的 agent：

- 只監聽本機迴路介面 `127.0.0.1`。
- `Host` 不是本機的請求一律拒絕，用來防止 DNS rebinding 攻擊。
- 帶有非本機 `Origin` 的請求一律拒絕，也不送任何 CORS header。所以遊戲執行時，你開的網頁沒辦法讀取或
  修改你的城市；Python、curl、PowerShell 這類 agent 工具不受影響。
- 會改動城市的工具都需要先有驗證過的 checkpoint 存檔。第一次使用請先拿測試用的存檔試。

## 🚀 快速開始

需求：Windows、Steam 版 Cities: Skylines 1、PowerShell、.NET Framework 編譯器（`csc.exe`，Windows 內建）、
Python 3.11 以上（請用該版本線最新的修補版）。建置文件網站另外需要 Node.js 24.22.1 或 26.12.1 以上，
也就是 2026-10-21 的安全修補版；`docs/.npmrc` 會拒絕更舊的版本。

建置並安裝 MOD。腳本會自動在所有 Steam 遊戲庫裡找遊戲；如果遊戲裝在別的地方，設定 `CS1_GAME_PATH`
或加上 `-GamePath`：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\build.ps1
# 遊戲在別處時：  $env:CS1_GAME_PATH = "E:\Games\Cities_Skylines"
```

到遊戲的內容管理員啟用 MOD、載入城市，然後測試：

```powershell
Invoke-RestMethod http://127.0.0.1:32123/health
```

透過啟動器的 Resume 載入最新的本機存檔。直接用 `--loadSave` 啟動時，Steam 會跳出要手動確認的視窗：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-resume.ps1
```

本機模型是選用的：啟動一個 OpenAI 相容的伺服器（LM Studio 預設是 `http://127.0.0.1:1234/v1`），
位址或模型名稱不同時，設定 `LOCAL_MODEL_URL` 和 `LOCAL_MODEL`。

要讓它在任何地圖上自己玩，用 [`scripts/loop/`](docs/guide/autonomous-loop.md)：它會自己劃區、
把舊區改成高密度、開發新地，保持水電接通，依每種需求挑合適的設施，並嘗試交通改善、量測效果，
只有需要規劃者決定的事才停下來。每座城市學到的東西只存在 `.runtime/` 底下那座城市自己的資料夾，
不會進 git。

```powershell
python .\scripts\loop\play.py 8 60 --reserve 8000
```

遊玩迴圈預設不呼叫本機模型；加上 `--model` 才會讓本機模型交叉核對每次 wake 的讀數。

完整的工具樹、API 清單和 agent 流程，請看[英文 README](README.md)。

執行單元測試（只需要 Python 標準函式庫，不必開遊戲）：

```powershell
cd scripts
python -m unittest discover -p "test_*.py"
```

## ⚠️ 狀態

實驗性專案，只支援 Windows 上的 Cities: Skylines 1。Bridge 會在遊戲執行緒上直接改動模擬中的物件，
請每次只做小改動、每一步都驗證，並備份重要的存檔。還做不到的事都寫在
[知識文件](docs/research/cs1-planning-knowledge-zh-TW.md)裡。例如，兩側已經有建物的道路要加寬時，
仍需要人來決定：遊戲會在新舊車道寬度的交接處重建劃區格子，站在那裡的建物可能失去劃區。

## 🙏 致謝

本 repo fork 自 Sunwood-ai-labs 的
**[cities-skylines1-agent-skill](https://github.com/Sunwood-ai-labs/cities-skylines1-agent-skill)**。
原專案做出了 CS1 agent 橋接 MOD、API、操作腳本和 skill
（[文件網站](https://sunwood-ai-labs.github.io/cities-skylines1-agent-skill/)），非常感謝！
本 fork 依原專案的 MIT 授權保留這些成果，並在上面加入了有型別的市長工具、驗證流程、本機模型分工和遊戲知識研究。

## 📄 授權

MIT，見 [LICENSE](LICENSE)，保留 Sunwood-ai-labs 原本的著作權聲明。
Cities: Skylines 是 Colossal Order / Paradox Interactive 的商標；本專案是獨立的玩家專案。
