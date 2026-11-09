# CS1 城市規劃知識

本文件整理 Cities: Skylines 1 base game 的規劃知識，來源是官方社群 wiki 與玩家社群。
目的是讓 mayor agent 不只「安全」，也懂得經營城市。

使用方式：

- 主 agent 每次 wake **不需要讀本文件**。可執行的部分已轉成
  [`references/cs1-gameplay-rules.json`](../../references/cs1-gameplay-rules.json)
  與 `scripts/mayor/gameplay_rules.py`，再由 `assess_city.planningHints`（最多四行）和
  `inspect_growth_capacity.growthPacing` 輸出。這樣知識不會增加每次 wake 的 token。
- 本文件是來源與理由的完整紀錄，只在調整規則或遇到新問題時查閱。
- 可信度：**wiki** = 官方社群 wiki（多數頁面標註最後驗證於 1.16/1.17）；
  **社群** = Steam／玩家指南的經驗法則；**實機** = 本專案 Bridge 實測。
- 任何知識都只是決策支援，不能取代 live unlock、native preview、checkpoint、
  settlement、focused QA 與 regression gate。

## 1. 前期經濟

| 知識 | 可信度 | 專案落實 |
|---|---|---|
| 前期支出大於稅收是常態，約 1,000 人才轉正 | 社群 | `early-deficit` hint |
| 住宅稅是最大收入來源；先讓住宅成長，延後昂貴服務 | 社群 | `early-deficit` hint |
| 預算 B<100% 時服務水準 = (B/10)²%；50% 預算 = 25% 效果 | wiki | `minimum_safe_budget()` |
| 供給過剩的水電先把預算調低，需求成長再調回 | wiki＋社群 | `*-budget-oversupply` hint |
| 預設稅率 9%；長期 >13–14% 會抗議；調高稅率會降低需求 | wiki | 規則常數，未自動使用 |
| 早期別開省電、省水、煙霧偵測政策，成本會讓城市虧損 | 社群 | `early-deficit` hint |
| 第一筆貸款在 Little Hamlet 解鎖（₡20,000，52 週，5%） | wiki | 尚未自動使用 |

實機佐證（55 人的城市）：每期支出約 865（水 585、電 265、道路 15），稅收約 167。
水 585 ≈ 抽水站 240＋排水管 320，已是最小配置，但使用率只有 0.6%。

實機驗證（62 人的城市）：白天水預算 100%→50% 後，Bridge 回報的供水容量
由 120,000 變為 30,000（恰好 25%，符合 (B/10)²），每週水費 586→309，每週淨額
−679→−393，10/10 建物仍有水與污水、無問題。白天與夜間預算是分開的設定，只調白天
不會得到完整節省。

## 2. 里程碑

| 里程碑 | 人口（依地圖） | 獎勵 | 主要解鎖 |
|---|---|---|---|
| Little Hamlet | 120–500 | ₡20,000 | 貸款、教育、垃圾、醫療 |
| Worthy Village | 240–1,000 | ₡20,000 | 區域、第二筆貸款、警察、消防 |
| Tiny Town | 360–1,600 | ₡20,000 | 公園廣場、高中 |
| Boom Town | 650–2,800 | ₡35,000 | 公車、高速公路、墓園、礦業特化 |

內部 milestone 名稱（例如 `Milestone1`）不等於顯示名稱，所以規則以「該里程碑解鎖的
capability 是否仍鎖住」判斷下一個目標，不猜名稱。社群建議用里程碑獎金蓋學校。

## 3. 劃區與成長

- 建物只在「有需求且有空的已劃區格」長出；建物正面必須貼路，block 深 4 格（32m）。（wiki）
- 低密度住宅每格約 0.2 戶（L1）到 0.42 戶（L5）；工業每格 1.0/1.5/2.0 個工作（L1–L3）。（wiki）
- 商業需求在「每 8 位市民 1 個商業工作」時為 0；工業需求主要反映失業率。（wiki）
- 前期 R:I:C 約 5:2:1 個 block。（社群，低可信度；以需求條為準）
- **死亡潮**：市民壽命約 6 年，一次大量劃區會產生同齡人口，約 6 年後集體死亡，
  壓垮殯葬服務。要「小批、隨時間持續」劃區。（wiki）

專案落實：`growthPacing` 讓空置供給**領先**需求，但每次 wake 有上限：

| 需求 | 目標空置格數 | 每輪上限 |
|---|---|---|
| 30–49 | 24 | 4 blocks |
| 50–69 | 96 | 4 blocks |
| 70–84 | 96 | 4 blocks |
| ≥85 | 144 | 4 blocks |

實機調整：原本目標 24/48/72 格、每輪 3 blocks 時，每個新 block 都在一次 wake
（約 25 遊戲日）內被吸收、需求仍 ≥50，空置格長期只剩 2–3 格，供給成為瓶頸，所以目標加倍、上限改為 4。
約 4,500 人時：三類需求都穩定在 40–49，可開發空格只剩 14／3／1，
門檻 50 讓城市在需求為正時停止成長，所以加上需求 30 起、目標 24 格（約一個 block）的一層。
約 9,300 人時：需求 53–60，目標 48 格每輪只開 1 個 block，而且當輪就被吸收（可開發空格一直是 26 格），
人口每輪只增約 20。改成 96 格後每輪開 3 個 block，人口每輪約增 53；快樂度、交通沒有變差。
空格以 `developableVacantCells` 計算，不含被建物擋在後排的永久空格。

舊規則是「入住率 ≥85% 才放行 1 個 block」，供給永遠落後需求，前期固定支出因此長期大於稅收。

## 4. 污染、噪音與工業配置

- 工業（農、林以外）、火力電廠、焚化爐會造成地面污染；住在污染地面的市民會生病，
  土地價值下降。（wiki）
- 噪音來自交通、工業、商業、電廠；樹木與隔音牆可降噪，地下道路完全消除噪音。（wiki）
- 住宅與工業隔約兩個街區，中間最好有樹或辦公區；工業應有直通高速公路的路線，
  避免貨車穿越市區。（社群／wiki）
- 抽水站放在排水口上游；水塔遠離地面污染。（wiki）

專案既有的 64m／96m 工業-住宅分離 gate 與此一致。

## 5. 道路與交通

| 道路 | 速度 | 造價/格 | 維護/格/週 | 解鎖 |
|---|---|---|---|---|
| Gravel Road | 30 | 20 | 0.19 | 開局 |
| Two-Lane Road | 40 | 40 | 0.32 | 開局 |
| Four-Lane Road | 50 | 60 | 0.80 | 開局 |
| Six-Lane Road | 60 | 80 | 0.96 | 開局 |

- 層級：高速公路 → 幹道（六線）→ 集散道（四線）→ 支路（兩線、碎石）。（社群）
- 兩條兩線道交會不會產生紅綠燈；四線以上會。紅綠燈是塞車主因。（wiki）
- 幹道少接路口、避免十字路口；路口太近會讓轉彎車道互相堵塞。（wiki／社群）
- 前期支路用碎石路即可，約 5,000 人或交通需要時再升級。（社群）
- 道路一旦沿線有劃區與服務，日後升級會很困難；主幹道要預留。（社群）

## 5a. 大範圍擴張的路網形狀（實機）

| 知識 | 可信度 | 專案落實 |
|---|---|---|
| 分區格 8×8m，最深 4 格（32m）；成長建物最大 4×4 格，正面必須貼路 | wiki | 既有 zone gate |
| 兩條平行道路之間剛好 8 格（64m）時，兩側 4 格深的分區正好填滿；兩線道寬約 16m，所以中心線間距約 80m | 社群＋計算 | 魚骨支路間距採 88m（留誤差） |
| 純棋盤會把車流集中在少數街道；優先 T 字路口，十字次之，五岔路避免 | etcwiki | 支路只做盡頭路 |
| 路口間距：低流量 1 個街區，高流量 ≥2 個街區 | etcwiki | 集散道每 88m 一個節點，只接低流量盡頭支路 |
| 工業放在高速公路入口附近，貨車不要穿越住宅；住宅離工業約 2 個街區 | etcwiki／社群 | 工業只沿 `industrial` 路延伸 |
| 外圍先用窄路，主幹道日後再升級；沿線已有建物時升級很困難 | 社群 | 集散道用 Basic Road，支路用 Gravel |

專案的 64m 路口 gate（block 中心離 ≥3 路節點或 ≥45° 彎 64m 內不可劃）會讓「路口很密」的
棋盤幾乎劃不了區。實機上產量最高的形狀是**魚骨**：

- 集散道（Basic Road）每段 88m、交錯 ±6° 偏轉（避免從盡頭共線延伸觸發 MoveMiddleNode）；
- 每個集散道節點向兩側拉 **Gravel Road 盡頭支路**（盡頭不算路口，所以只有靠集散道的
  第一個 block 被 gate 排除），第二段再偏 6° 延伸；
- 實機一次建 5 段集散道＋6 條支路（約 2.1km），花費約 ₡5,600，得到 31 個可劃 block。

成本實測（raw 為內部百分位）：Basic Road 96m = 48,000（₡480）；Gravel Road 200m = 50,000（₡500），
是 Basic 的一半。緩坡上的 Gravel 支路容易低於 0.12m 離地下限，要縮短到 64m 或改路線。

住宅需求會隨就業下降：失業率 19% 時住宅需求從 77 掉到 37，工作需求升到 63。
擴張時要同時補工作（工業／商業），否則新住宅區不會長。

**原生 NetTool 的兩個靜默副作用（實機）**，`preview-network` 原本都回報 valid：

- 新地面道路壓到的成長建物（住宅、商店、工廠）會被直接拆掉，只有「重要」建物才會觸發
  ObjectCollision。
- 新地面道路跨過既有電線時，會把**整段電線刪除**：曾刪掉剛蓋好的 344m 電線，
  讓 4 間商店斷電。電線要**最後**拉（電線跨既有道路沒問題）；延伸道路前要檢查電線。
  `propose_frontage_road` 也沒考慮電線，曾提出會切斷抽水站供電線的工業延伸。

兩者現在都在 `RoadCommands` 的 preview／build 先擋下（`buildingsUnderRoad`、`powerLinesCrossed`）。
管線接頭、電線桿、橋墩等掛在網路節點上的建物（`Building.Flags.Untouchable`）不算在內。

魚骨擴張的兩個順序教訓：

- 集散道末端節點要**立刻**拉支路。末端節點只有兩條路、偏轉 6°，不算路口，所以下一輪
  成長就會把集散道兩側劃成商業，商店一長出來，兩條支路走廊都被擋住。
- 從工業路盡頭往上坡延伸時，preview 每次都 valid，settled 驗證卻 5 次都失敗
  （路面最低低於地形 4–16cm，抬高端點只會讓最大落差變大）。同時間別處的 Gravel 支路正常，
  所以不是部署問題，是那一帶 6% 上坡加上工廠整地。換方向，不要在同一處重試。
- 早期拉的電線會擋住後來的道路方向：供抽水站的電線斜跨過去，正好壓在工業路盡頭。
  做法是**先改線再拆舊線**：從工業路南側有電的商店拉一條新線到取水口（終點要在取水口的
  32m grid 半徑內，又不能進到建物 footprint ＋4m 的電塔淨空），確認取水口仍有電、水容量不變後，
  才以 `confirmRisks` 拆掉舊線（風險只有預期中的 networkCutSegment）。之後西延伸一次就過。

跨越高速公路（實機，人口 6,500）：

- 要用立體交叉，不能平面相交。`build_network` 的 `startHeightOffset`／`endHeightOffset` 讓
  高架、橋梁、坡道、隧道 prefab 各端點高度不同，這樣一段就能當坡道；地面道路帶非零偏移會被拒絕。
  三段的做法：地面 → 爬升坡道 → 跨越高架 → 下降坡道。
- 從節點直線延伸（共線）會觸發 MoveMiddleNode 拒絕。每段至少偏轉 6–8°。
- 下降坡道的終點偏移要留 0.15，不要 0。偏移 0 時只剩 0.02m 淨空，接不上地面道路
  （地面道路需要 ≥0.12m）。
- 建造前就被拒絕（例如 MoveMiddleNode）時沒有蓋任何東西，工具會記成
  `noConstructionAttempted`，不擋其他道路。等待遊戲執行緒逾時則仍要求復原驗證。
- 高速公路之間的窄長平地適合一條集散道加沿長邊的支路（實機 12 個街區、374 格）。
  被高速公路圍住的完全平坦大平原適合整片做工業園：遠離住宅，貨車直接上高速公路。
- 坡道坡度上限約 0.25（`SlopeTooSteep`）。dead-end 離高速公路太近、直上不夠長時，
  就讓坡道沿著高速公路平行爬升（50m 升到 +10），再轉向跨越。
- 用道路名稱判斷土地用途很脆弱：工業園的道路名稱裡沒有 `industrial`，
  整個工業園被當成住宅來劃區。做法是另外記錄各區的座標範圍與用途，優先於道路名稱。
- 跨到高速公路另一邊的新區不和舊電網相連，第一批住宅一出現就斷電（9 棟）。用
  `propose_power_line_connection` 從有電的區域拉一條 159m 電線，一次就全部通電。
  遊玩迴圈偵測到缺電時可以自動處理：提案 → checkpoint 建造 → 重查覆蓋。
  （遊玩迴圈預設不呼叫本地模型；`play.py --model` 才會讓它交叉核對每次 wake 的讀數。）

## 5b. 道路等級與升級（實機）

選路種的依據：

| 用途 | 道路 | 理由 |
|---|---|---|
| 地方支路（dead-end 魚骨刺） | Gravel Road（10m 寬） | 最便宜，只服務兩側幾個街區 |
| 集散道 | Basic Road（16m） | 承接支路車流，兩側可劃區 |
| 壅塞的集散道或工業主幹 | Small 4 Lane Road（16m，四線道） | 和 Basic Road 一樣寬、一樣價格，升級不必拆兩側建物 |
| 主要幹道 | Medium Road 或 Large Road（32m） | 寬度加倍，會壓到兩側建物，最好新建時就用 |
| 跨越障礙 | Basic Road Elevated | 只有高架、橋梁、坡道、隧道 prefab 能帶高度偏移 |

- 用交通數據決定升級：`inspect_traffic`（`minimumDensity` 64）列出最壅塞的路段。實機
  壅塞集中在工業主幹：所有工業貨車都經過同一個高速公路入口路口，
  整條工業主幹的壅塞度都是 100。
- **升級會重建 zone block**（Bridge 和原生 NetTool 都是 release 舊 segment 再建新的）。
  在兩種規格的交接 node 旁，那一排格子會變成無效，或從快取還原時掉成 Unzoned。
  原生 `Building.CheckZoning` 要求佔地每一格都有有效、非共用、劃區相符的格子；
  `ZonesUpdated` 的建物在模擬時有 1/10 機率被檢查，不通過就標成 `Demolishing`。
  實測升級一段工業路後，旁邊一座工廠腳下那排失效；只要恢復模擬，它就會被拆掉。
- 因此 `upgrade_road` 升級完會用原生 `CheckZoning` 檢查附近建物：Unzoned 的格子用
  `repair_building_zoning` 補回建物自己的劃區；仍有建物不通過就回報失敗，
  這時要在恢復模擬前還原 checkpoint。
- 升級一整串路段時，中間的 node 在兩側都升級後會恢復正常：一串 12 段途中失敗的
  中間 node，做完都恢復了。問題只留在兩端的規格交接點。
  所以整串升級時中途不跑模擬，整段做完才做最終驗證和修補。
- 交接點旁必須沒有建物才安全，但「50m 內有幾棟建物」預測不準：有的交接點旁有 2 棟卻沒出事，
  三岔、四岔路口旁的建物則在路口 40m 內就被劃區、長出建物。
  終點選在路口時剩 3 棟不通過，再往下延伸變成 5 棟，不會收斂。
  唯一可靠的作法是：升級 → 原生驗證 → 不通過就載回 checkpoint。
- 實際只保留驗證通過的入口 3 段。入口匝道段的壅塞度從 100 降到 40；下游仍是兩線道，
  連接段仍然是 100。要真正疏解，就得在主幹終點拆掉幾棟工廠，或幫工業區另開第二個
  高速公路出入口。
- 升級後的 segment 原本會換成隨機街名，但遊玩迴圈靠名稱判斷用途，所以現在
  `upgrade_road` 會保留自訂名稱（`customNameKept`）。

### 路段長度上限：RoadAccessFailed（遊戲原始碼 ＋ 實機）

- 原生道路工具一拖就把路切成 `floor(長度/100)+1` 段，每段都短於 100m；Bridge 的
  `build_network` 一次只建一段，所以以前可以建出 200–300m 的單一路段。
- `PathManager.FindPathPosition` 只會找「至少一個端點 node 在查詢點 ±(maxDistance+64m)
  範圍內」的路段。救護車、消防車找建物時 maxDistance 是 32m，所以超過約 192m 的路段，
  中間的建物車輛找不到。路徑失敗時，遊戲會在建物標上 `RoadAccessFailed`（之後每次模擬
  有 1/16 機率清掉，所以會一直反覆出現）。
- 一個路段兩側最多各兩個 zone block（起點、終點各一個），每個 block 最多 8 格（64m）。
  超過 128m 的路段，中間那一截沒有任何劃區格子。
- 現在的規則：
  - Bridge 拒絕建超過 160m（含 1m 誤差）的道路段。
  - 道路異常會列出 `overlongRoadSegment`。
  - `assess_city` 遇到 `RoadAccessFailed` 時會提示要切路。
  - `split_road_segment` 用原生 `NetTool.SplitSegment` 把路段切成最少幾段、每段 ≤160m，
    切點落在 8m 格線上，旁邊建物的格子才不會錯位。
- 實機：兩條 290m 碎石支路切開後，`RoadAccessFailed` 不再出現。但路段兩端的 zone block
  會重建，端點路口旁的轉角格子可能換人擁有，6 次切路中有 2 次害轉角建物失去劃區支援。
  工具會偵測到並回報失敗，這時要載回 checkpoint。只有確實有建物出現 `RoadAccessFailed`
  的長路段才需要切。

## 5d. 交通規劃流程（wiki ＋ 實機）

wiki 的關鍵規則：車輛在出發時就選好「不塞車時最快」的路線與車道，途中遇到塞車
**不會改道也不會換車道**。所以繞道只有在不塞車時也比較快才有用；新路剛蓋好可能沒人走。
路口太近時轉彎車道會重疊；單行道讓同方向車道數加倍；升級道路可以把轉彎車與直行車分開。

工具對應：

| 步驟 | 工具 | 看什麼 |
|---|---|---|
| 找壅塞 | `inspect_traffic` | `density` 100 的路段；`totalTrafficFlow / maximumTrafficFlow` 是全市流量比 |
| 誰在用 | `inspect_segment_routes` | 車種、方向、從哪段進、往哪段出、起訖格（遊戲 Traffic Routes 視圖） |
| 自動偵測 | `assess_traffic_congestion` | 全市流量比、把共用 node 的壅塞路段併成熱點，列出每個熱點可試的路口與可升級路段 |
| 路口設定 | `inspect_junction`、`set_junction_control` | 紅綠燈開關、支路停車標誌（遊戲 Junctions 分頁） |
| 量測式試改 | `trial_junction_control` | 改一個路口（紅綠燈開關，或安靜支路上的停車標誌），改前改後各量一次，沒有明顯改善就自動改回 |
| 整串拓寬 | `widen_road_chain` | 熱點路段整串升級成同寬四線道，整串做完才判斷劃區，補不回的建物在安全時主動拆除 |
| 加容量 | `preview_road_upgrade`、`upgrade_road` | 同寬（16m）升級：Basic → Small 4 Lane、→ Small 4 Lane 1 Way |
| 拆遷 | `inspect_building_zoning_support`、`preview_bulldoze_impact`、`bulldoze_entity` | 升級後失去劃區支援的建物 |

實機教訓（人口約 7,000）：

- 魚骨狀住宅區的支路只接到幾條集散道，集散道全部倒進同一條兩線東西幹道；
  連區內短程旅次也得繞到幹道再折返。幹道 14 段 density 100。
- 幹道中間 node 都是 degree 2（沒有支路），升級成 Small 4 Lane 仍然會讓 zone block 重建，
  約 10% 劃區格失效（275→245），幾棟小型店面／工廠／住宅失去支援。
  確認可以拆遷時，用 `inspect_building_zoning_support` 列出、`preview_bulldoze_impact`
  確認安全後主動拆除，比恢復模擬後讓遊戲隨機拆更可控。之後全市 0 棟不通過。
- 升級後全市流量比 83% → 約 91–95%，density 100 的路段從 14 段降到 0（壅塞移到高速公路入口）。
- 高速公路入口：`inspect_segment_routes` 顯示 198 輛（160 輛貨車）有 118 輛左轉進一段 32m
  的連接路，兩個路口太近，排隊回堵到高速公路。
  - 在四岔路口開紅綠燈反而更差：全市流量 0.89 → 0.80，南向支路 45 → 92，已復原。
    無號誌路口在 CS1 常常比紅綠燈好，要量測，不要用猜的。
  - 把入口段升級成 Small 4 Lane 1 Way（一樣 16m，畫線方向就是行車方向）讓左轉與右轉分道：
    高速公路 100 → 66、入口段 100 → 81，全市流量回到約 0.93。
- A/B 量測：同一組路段在 2 分鐘（3 倍速）內取 6 次平均 density 與流量比，
  改動前後各量一次，變差就用同一個工具改回去。
- 自動化：遊玩迴圈每輪呼叫 `assess_traffic_congestion`。熱點要連續 3 輪都在才處理（流量隨時段變動）。
  - 每輪最多試一個路口：先拆紅綠燈，其次在安靜支路放停車標誌（讓排隊的主路先走），最後才試加紅綠燈。
  - `trial_junction_control` 只保留明顯改善：受觀察路段平均 density 至少降 5，且全市流量比降幅不超過 0.01；否則改回。
  - 路口都試過還塞，就用 `widen_road_chain` 把熱點的 Basic Road 整串升級成 Small 4 Lane（同 16m 寬）。
    劃區等整串做完才判斷（中間 node 兩側都升級後會恢復）。建物腳下的格子能補就補；
    仍不通過的建物遊戲會隨機拆掉，所以拆除預覽安全就先主動拆，讓空地重新長。只有拆不安全時才要載回 checkpoint。
  - 拓寬後還塞，就由 agent 依 `inspect_segment_routes` 的起訖規劃新路線（繞道、第二出入口、單行道對）；不必等人決定。
  - 實機第一次試驗：在熱點路口的安靜支路放停車標誌，量測沒有改善，工具自動拆回，regression 通過。

### 工業區專用高速公路出入口（實機）

社群攻略最一致的建議：讓工業區有自己的高速公路出入口，貨車不必穿過市區。

- 先用 `inspect_segment_routes` 的 `originDestinationPairs` 看誰在用壅塞路段。
  起點格＝終點格代表貨車出口貨物後「回家」；入口回堵的車大多是遠端工業區的回程貨車，
  因為那片工業區沒有任何高速公路出入口，所有貨車都從唯一的市區入口進出。
- 找高速公路經過工業區、且已有地方道路以橋或地下道穿越的位置，做菱形交流道：
  兩個方向的車道各一條出口、一條入口，接到穿越道路兩端的地面 node。
  高速公路每 ~95m 有既有 node，匝道可以直接接上，不必切開路段。
- 匝道要從車道外側離開；跨越另一側車道的高架匝道會被兩車道中間約 10m 的分隔帶擋住
  （橋墩 `ObjectCollision`），從同側出入、利用既有穿越道路比較可行。
- 地圖原生（Original）的高速公路 node 貼地，與地形高差 0。從它出發的匝道要分段：
  接觸原生 node 的那段 ≤64m（原生 node 過渡段可以和該 node 一樣低），
  **先蓋地方道路那一段**（新 node 可以抬高），再蓋接到高速公路的那段；
  反過來蓋的話，第一段的新 node 會沉到 0.1m，第二段過不了 0.12m 的預覽門檻。
- 路線上的電線也會擋住地面道路。孤立的一段電線（兩端 degree 1）仍可能是跨越高速公路
  供電的唯一連線：拆掉後 22 棟停電。用 `propose_power_line_connection` 對停電建物
  另找一條不經過匝道的電線，蓋好後全數恢復，再蓋匝道。
- 實機結果（菱形交流道 8 段、拆 2 棟工廠）：市區入口高速公路 100 → 4.5、
  入口段 81 → 11、東向幹道 78 → 11、工業區道路 100 → 13，全市流量約 0.95。

## 5c. 找新區平地與跨鐵路（實機教訓）

- `/state/map-analysis` 的 `buildable` 只代表坡度 < 0.25（建物放置上限），**不是平地**。
  實機曾把一座高 90m、坡度約 19% 的山丘當成空的平地。魚骨路網要看 `slope`：
  < 0.06 才算平地，0.06–0.12 是緩坡。`/state/networks` 也包含看不見的
  Airplane／Ship Path 和地下水管，畫地圖找空地時要排除，否則會把飛機航線當成道路。
- Bridge 不能做平交道：`build_network` 的 `CreateNode` 只以既有 node 為端點
  （`maxSegments=1`），在既有路段中間穿越另一條網路會回 `ObjectCollision`。原生做法是
  點在路段上觸發 `SplitSegment`，但它會對鐵路的 Middle node 呼叫 `MoveMiddleNode`
  改寫既有幾何。要跨鐵路就用三段高架立交：斜向爬升 0→10m（約 64m，坡度 0.16）、
  高架 10m 跨越（約 9m 淨空）、下降到 0.15m 落地。跨線橋也不會被火車擋住車流。
- 平台邊緣的**凸形肩坡**：垂直等高線的 200m 支路中段一定低於地面，
  preview 的容許窗口只有離地 0.12–0.30m。做法是集散道從平台邊緣（坡度最小處）出發、
  垂直等高線用 88m 短段，支路**沿等高線**走（剖面近乎直線）。
  從肩坡上的 junction 出發的短段，preview 預測離地 ≥0.12m，實際卻是 −0.13m。
- **道路會把地形削平**：遊戲會把道路經過處繪製出來的地形（detail）壓到道路的人行道高度，
  原始高度圖（raw）不變。預設的 0.12m 離地門檻是拿 raw 量，所以起伏的自然地圖上大多數直線道路
  都會被擋（某張地圖抽樣 105 條 88m 道路有 97 條不過）。實測道路比 raw 低 0.49、1.05、1.32m 時，
  繪製地形都在人行道高度 ±0.06m 內，也正常長出劃區格子。preview 的 `terrainCut.carvable` 表示
  道路只會切進起伏（最深 1.3m），`build_network` 加 `allowTerrainCut: true` 就以切削方式蓋，
  settled 改驗繪製地形是否跟著道路（±0.15m），不合格照樣回滾。
- 既有集散道兩側已長滿房子時，開 T 字路口要先拆 1–2 棟（先 `preview_bulldoze_impact`）。
- preview 與 settled 的地形差距：平地上 16 段的最大偏差差距 ≤ 0.04m、最低離地差距
  ≤ 0.03m，相較 Bridge 預留的 0.7m 偏差預備量非常保守；但從 junction 出發的那段誤差達
  −0.26m。要累積非平地樣本後再校正。
- 這個誤差的原因已找到：settled 道路在 End、Bend、Junction node 一律把路段方向壓平，
  只有筆直的 Middle node（兩段幾乎成一直線）保留坡度。所以爬升 Δ 的路段在低端附近
  會比坡面低約 0.096Δ（實測 3.6% 坡、y = a + Δ(3t²−2t³)，每個樣本誤差 ≤0.011m）。
  從既有 Bend／Junction 出發時，路段還會從路口轉角開始（實測 145° 轉角偏移 4.8m），
  高度跟 node 一樣、位置卻往上坡移，起點又再低 坡度×偏移。
  preview 的 `terrainProfile.settledShape` 現在會取樣這條兩端壓平的曲線（轉角偏移用路寬一半估），
  低於門檻就 `safe:false`。實測三段原本 preview 通過、settled 失敗的路，現在 preview 就擋下；
  80 個從死路端點出發的直線與分支 preview 沒有一個因此被新擋。
  做法：上坡要從既有路段**直線延伸**（Middle 保留坡度），或沿等高線走；不要在坡上從轉角或路口岔出。
- 地面道路 immediate 剖面不安全時，以前 Python 層直接丟例外，跳過 Bridge 的 settled
  驗證與本地回滾，只能重載 checkpoint。現在 immediate 剖面只是暫定值，由 settled
  驗證決定，失敗時由 Bridge 釋放路段並退款。

## 6. 服務

| 服務 | 解鎖 | 代表建物（造價／維護） | 規劃重點 |
|---|---|---|---|
| 教育 | Little Hamlet | 小學 ₡10,000／160，300 人 | 容量 ≥ 適齡人數；教育影響升級 |
| 醫療 | Little Hamlet | 診所約 ₡10,000／400，100 人 | 污染與噪音會使人生病 |
| 垃圾 | Little Hamlet | 掩埋場 ₡4,000／160 | 放在工業區；未收垃圾會造成污染與廢棄 |
| 警察 | Worthy Village | 警察局 ₡12,000／480 | 失業與不滿會提高犯罪 |
| 消防 | Worthy Village | 消防站 ₡12,000，半徑 700m | 工業火災較多；放在工業與住宅之間可同時涵蓋 |
| 殯葬 | Boom Town | 墓園 ₡8,000／160，3,000 | 屍體未收會導致廢棄；注意死亡潮 |

- 工業與辦公的升級權重：貨運 +1、消防 +0.5、大眾運輸 +0.333，警察／醫療／殯葬
  各 +0.2；噪音 −0.143、地面污染 −0.167。（wiki）
- 住宅升級看土地價值與教育；土地價值由服務覆蓋、公園、靠水提高，被污染、噪音與犯罪拉低。（wiki）

### 6a. 實機（人口 1,700–2,400）

- 商業 frontage 會比住宅先用完：一條主幹 + fishbone 只靠 collector 兩側與轉角店。
  做法是在住宅與工業之間另開商業短街（兩側都劃商業），
  同時當工業噪音的緩衝帶；主幹旁已長滿住宅時，直角接出會被房子擋住，斜接才過。
- 等級 2 商業缺高中學歷員工會廢棄（`NoEducatedWorkers` → `Abandoned`）。
  廢棄建物不會自己恢復；確認 `preview_bulldoze_impact` 安全後拆除讓地塊重長。
- 電力 85% 時加風力：同一排 62m 間距的風車會互相導電，不必再拉電線；
  每台約 +7,900 容量。
- 水的 budget 75%→100% 讓容量從 68,400 變 120,000（非線性），比加取水口便宜；
  但 120%→150% 只從 139,200 變 150,000，超過 100% 後幾乎沒有效益。
- 加第二組取水口／排水口：`propose_water_pair_candidates` 會把排水口推到下游端點，
  `propose_water_service_connection` 只接用水建物，所以用 `build_water_pipe` 的完整系統計畫：
  把既有主管線（由 `/state/networks` 的 Water Pipe 圖 BFS 出來）當成 index 0..k 的
  `sharedTrunk`（建造時會比對 live 管線、不重建），再接一段 stub 讓主管線端點變成三岔，
  最後依序建 intake／outlet branch。branch 端點要用 `inspect_water_pair` 預覽的
  `networkPaths` 接點（2m 容差）；branch 不能和既有管線平行接到同一節點（ObjectCollision）。
  實機：水容量 150,000 → 300,000。取水與排水建物很耗電，要同時加發電。
- 第三組以後改用 `connect_water_facility`，不必再組整套計畫：
  1. 沿著既有取水口的岸線，用 preview 的 `waterPlacement.closestWater.found` 掃描，找會碰到水的位置。
     （`propose_water_pair_candidates` 在已有水系統時，常回報不在同一段可用水域。）
  2. 用 `place_water_facility` 放置。放第二個角色時，計畫裡已建好的那一個要帶 `buildingId`，
     否則重驗時會撞到自己。
  3. 用 `connect_water_facility`（`pairBuildingId` 指向已接管的另一個角色）從原生接點拉一段管到
     最近的 live 管線 node。
  - 排水口若放在既有管線旁，放置時就會自動切開那條管線接上，不必再拉管。
  - 實機：水容量 240,000 → 360,000。
- 掩埋場／墓園滿了（`CapacityFull`）就不再派車收集，只剩未滿的設施在跑全市；
  合計存量（例如 78%）會掩蓋「9 座裡 7 座已滿」的情況，要看 `storage.fullFacilities`。
  實機：只剩 2 座在收時，遠處約 140 棟出現 `Garbage`。只有掩埋場解鎖時，
  拆除已滿的掩埋場會丟棄存量（`bulldoze_entity` 需 `confirmRisks`），在原位重建同款設施
  可沿用已驗證的道路接入；離垃圾集中區太遠的已滿設施直接拆掉省維護費。
- `place_water_facility` 放第二座時要在 pair 裡帶第一座的 `buildingId`，否則會把已放好的
  建物當成碰撞而回 `intakePlacementInvalid`。
- 大型服務建物（墓園 10×8、公車 depot 6×6）在既有道路旁幾乎找不到空地：
  先用 Python 濾掉 footprint 半徑內有建物或其他道路的位置，再交給
  `preview_building_candidate` 當硬閘門；中段道路旁的空地通常只剩轉角或新路。
- （已移除）舊的劃區閘門 `nearConnectedRoadEnd`：zone block 中心距離「≥3 路或急彎」節點 64m 內
  一律不劃；實機證實它讓格狀街區（邊長約 96m）幾乎整區不能劃，現在照玩家的方式劃到路口，
  重疊的格子由遊戲自己判為無效。以下是當時的觀察：88m 的 collector 段兩端都是路口，所以 collector 兩側幾乎沒有可劃 block；
  可劃的 frontage 主要來自 **長 dead-end 支路**（160–200m 支路約 2/3 的 block 可劃）。
  商業短街要做成 dead-end 並延伸（每段 6–10° 小轉折算平順延續，不觸發閘門），
  不要在短距離內再接路口；45° 轉折的連接路兩側會整段失效。
- 從已長滿工廠的主幹接支路時，先用「忽略建物碰撞」的條件確認地形與 preview 會過，
  **再**拆擋路的建物；實機曾先拆後驗，3 條支路地形不過，白拆 5 棟工廠。
- 地形：主幹旁已有建物時改走地圖上的平原。`/state/map-analysis?minX&maxX&minZ&maxZ&resolution`
  可畫出 slope／水／已占用的 ASCII 地圖（見 §5c 的坡度分級）。新區接在既有路口時，若相鄰路段 settled 後低於地形
  （`adjacentTerrainSafe=False`）會被回滾；改從 dead-end 延伸並用 48–64m 短段。
- Milestone 5（實機地圖約 2,400 人）解鎖公車、墓園、Eldercare、Child Health Center、
  中／大型道路與獨特建築；`deathCare.capacity` 放完墓園後仍可能讀到 0，以
  facility flags `Active` 為準。

### 6b. 實機（人口 9,000–14,000）：土地用完之後

- 新地塊要等 milestone 才能買：Milestone 8 時面積數 4/9，但 `canUnlock` 全是 false。
  在這之前，成長只能靠提高既有土地的效率。
- 住宅升密度：`zone_block`（`preserveOccupied: false`）把低密度區塊改成 `ResidentialHigh`。
  遊戲約 30 秒內拆掉舊屋，1–2 分鐘內長出高密度住宅。
  （舊）路口轉角的區塊曾被 safety gate 跳過；閘門已移除，`junctionClearanceSafe` 只作參考。
- 先把碎石支路升級成 Basic Road 再升密度並不可靠：11 段裡 8 段破壞了轉角劃區。
  每段都要單獨驗證，第一次失敗就停下並載回 checkpoint。
- 公園和服務涵蓋率：原本沒有任何公園時，放 6 座 Regular Playground、在最遠的住宅區補一間診所。
  兩輪內人口 13,026 → 13,849（+6%）、地價 14 → 16、快樂度 77 → 80、病人 36 → 27。
  診所使用率只有 12%，但還是有效：缺的是涵蓋距離，不是容量。
- 大學：需求約 4,900 而城市沒有大學，蓋好後人口每輪成長從約 60 升到約 200。
  維護費會讓淨收入下降約 25%。
- 墓園和掩埋場：最遠的住宅區離墓園 1.4–1.9km 時，有 19 棟出現 Death 問題，在中間補一座墓園就解決了。
  滿的設施原地重建（儲存內容會丟棄）比加焚化廠便宜得多。
  遊戲對滿的墓園也標 `LandfillFull`，所以要用「CapacityFull 旗標或 LandfillFull 問題」判斷是否滿了。

## 7. 大眾運輸（Boom Town 後）

- 公車站需先蓋 depot（約 ₡30,000）；每輛公車載 30 人，運行成本約 ₡12。（wiki）
- 站牌不要太靠近路口；雙向路線需要迴轉點；兩線雙向道上停靠的公車會擋車流。（wiki）
- 實機：depot 剛放好時 `preview_transport_plan` 會回 `depot.buildingId=0`，
  模擬幾秒後才偵測得到。單一主幹城市先開一條沿主幹的環線（約 175m 一站、
  10 站），終點接工業區，同一條路來回即可。

## 8. 尚未落實、待實機驗證

- 稅率調整對需求的定量影響：wiki 表示未知，先不自動化。
- 里程碑在此地圖的實際人口門檻：需要從 `UnlockManager` 讀取，目前只有範圍。
- 工業-住宅「兩個街區」緩衝的距離換算：沿用既有 64m／96m gate，未另訂數字。

## 來源

- [Milestones](https://skylines.paradoxwikis.com/Milestones)、
  [Economy](https://skylines.paradoxwikis.com/Economy)、
  [Water and sewage](https://skylines.paradoxwikis.com/Water_and_sewage)、
  [Power](https://skylines.paradoxwikis.com/Power)、
  [Zoning](https://skylines.paradoxwikis.com/Zoning)、
  [Zones](https://skylines.paradoxwikis.com/Zones)、
  [Roads](https://skylines.paradoxwikis.com/Roads)、
  [Traffic](https://skylines.paradoxwikis.com/Traffic)、
  [Land value](https://skylines.paradoxwikis.com/Land_value)、
  [Pollution](https://skylines.paradoxwikis.com/Pollution)、
  [Education](https://skylines.paradoxwikis.com/Education)、
  [Healthcare](https://skylines.paradoxwikis.com/Healthcare)、
  [Garbage](https://skylines.paradoxwikis.com/Garbage)、
  [Happiness](https://skylines.paradoxwikis.com/Happiness)、
  [Industry](https://skylines.paradoxwikis.com/Industry)、
  [Crime](https://skylines.paradoxwikis.com/Crime)、
  [Policies](https://skylines.paradoxwikis.com/Policies)、
  [Public transport](https://skylines.paradoxwikis.com/Public_transport)、
  [Districts](https://skylines.paradoxwikis.com/Districts)
- [Steam：前期負收入討論](https://steamcommunity.com/app/255710/discussions/0/1732090362063327180)
- [etcwiki：Tips and FAQ](https://etcwiki.org/wiki/Cities:_Skylines_-_Tips_and_FAQ)
- [PMC Tactical：CS1 Roads](https://cities-skylines.pmctactical.org/cs1/roads.php)
- [guidestrats：Traffic guide](https://guidestrats.com/cities-skylines-traffic-guide/)
- [Windows Central：Beginner's guide](https://www.windowscentral.com/cities-skylines-city-sim-beginners-guide)
