# Mayor tools 分群與按需載入

Mayor adapter 原本已支援六個核心工具的 `--profile mayor` 與精確名稱的
`--names`，但領域工作缺少介於兩者之間的選擇：如果 agent 尚未決定下一個
確切 tool，就容易一次載入全部的 schema。

現在可先讀取不含完整 schema 的 cluster 目錄：

```powershell
python scripts/mayor_tools.py clusters
```

接著只載入當前目標需要的領域：

```powershell
python scripts/mayor_tools.py definitions --cluster roads
python scripts/mayor_tools.py definitions --cluster water
python scripts/mayor_tools.py definitions --cluster development
```

如果已經知道下一個呼叫，仍應使用最小的精確集合：

```powershell
python scripts/mayor_tools.py definitions --names preview_network inspect_road_result
```

建議 workflow 是 `core → 一個 domain cluster → exact names`。Cluster 是降低
提供給模型之 schema 範圍的 progressive disclosure，不是權限邊界；實際呼叫
仍會執行既有參數驗證、native preview、checkpoint、settlement 與 regression gate。

Manifest 位於 `references/mayor-tool-clusters.json`。工具可合理地屬於多個
cluster，例如 `build_network` 同時服務道路與電力線；載入時會 fail closed 地拒絕
未知工具、cluster 內重複、未支援版本，以及任何未被分類的 typed tool。

`clusters` 回傳 compact JSON schema 字元數與相對完整 70 工具的縮減比例。
這是與 tokenizer 無關、可重現的大小代理，不宣稱等於實際 token 數。首次實作的
量測為：完整 62,221 字元；`core` 2,541（減少 95.9%）、`roads` 11,281
（減少 81.9%）、`water` 14,740（減少 76.3%）、`economy` 1,863
（減少 97.0%）。

注意：native `constructionCost` 使用遊戲內部金額單位。規劃器現在回傳唯一
`candidateId`，並以 `cashPolicy.constructionCostDisplay`、`projectedCashDisplay` 與
`reserveDisplay` 明確執行保留金 gate，避免 agent 把 raw 48,000 誤讀為顯示金額 48,000。
