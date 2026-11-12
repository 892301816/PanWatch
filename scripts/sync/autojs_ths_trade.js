// PanWatch 成交同步 —— AutoJs6 无障碍读取同花顺「成交」页
//
// 适用：你用的是同花顺手机端（实盘或模拟盘都行），但券商不发成交短信/推送。
// 原理：AutoJs6 打开无障碍服务后，读取当前页面所有 TextView 文本，
//       用正则拼出一笔成交，POST 到 PanWatch 的 /api/portfolio/trades/ingest。
//
// 使用步骤：
//   1. 安装 AutoJs6（GitHub: SuperMonster003/AutoJs6），导入本脚本；
//   2. 打开同花顺 → 交易 → 查询 → 成交（或当日成交）页面；
//   3. 修改下面 CONFIG 的 api / token / accountId；
//   4. 运行脚本，看到「已记录」即可；之后可用 AutoJs6 定时任务或 MacroDroid 启动时运行。
//
// 注意：页面改版/字段缺失会解析失败，脚本会提示而不会乱记；同一天重复运行靠本地
//       storages 去重，服务端还有成交编号/复合键二次去重。

"ui";

const CONFIG = {
  api: "https://你的域名/api/portfolio/trades/ingest", // 末尾不要带 /
  token: "pwmcp_在设置-交易同步里创建", // scope: trade:write
  accountId: 1, // 设置 → 交易同步 里的账户 id
  market: "CN",
};

const STORE_NAME = "panwatch_ths_sync";

function collectTexts() {
  const nodes = className("android.widget.TextView").find();
  const parts = [];
  for (let i = 0; i < nodes.size(); i++) {
    const text = nodes.get(i).text();
    if (text && text.trim()) parts.push(text.trim());
  }
  return parts.join(" | ");
}

function parseTrade(text) {
  const normalized = text.replace(/：/g, ":").replace(/，/g, ",").replace(/\s+/g, " ");
  const actionMatch = normalized.match(/(买入|卖出)/);
  const codeMatch = normalized.match(/(?<!\d)(\d{6})(?!\d)/);
  const qtyMatch =
    normalized.match(/(?:成交数量|数量|成交股数)\s*:?\s*(\d+)/) ||
    normalized.match(/(\d+)\s*股/);
  const priceMatch =
    normalized.match(/(?:成交价格|成交均价|成交价|价格)\s*:?\s*([\d.]+)/) ||
    normalized.match(/@\s*([\d.]+)/) ||
    normalized.match(/以\s*([\d.]+)\s*元/);
  const dateMatch = normalized.match(/(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})/);
  const timeMatch = normalized.match(/(\d{1,2}:\d{2}(?::\d{2})?)/);

  if (!actionMatch || !codeMatch || !qtyMatch || !priceMatch) return null;
  const action = actionMatch[1] === "买入" ? "buy" : "sell";
  const [_, y, m, d] = dateMatch || [];
  return {
    action,
    symbol: codeMatch[1],
    quantity: parseInt(qtyMatch[1], 10),
    price: parseFloat(priceMatch[1]),
    trade_date: dateMatch
      ? `${y}-${String(m).padStart(2, "0")}-${String(d).padStart(2, "0")}`
      : null,
    time: timeMatch ? timeMatch[1] : "",
    source: "notification",
    account_id: CONFIG.accountId,
    market: CONFIG.market,
    note: "AutoJs6 同花顺成交页",
  };
}

function signature(trade) {
  return [trade.trade_date || "", trade.symbol, trade.action, trade.quantity, trade.price].join("|");
}

function alreadySynced(key) {
  const store = storages.create(STORE_NAME);
  return !!store.get(key);
}

function markSynced(key) {
  storages.create(STORE_NAME).put(key, Date.now());
}

function main() {
  const raw = collectTexts();
  if (!raw) {
    toastLog("页面上没有读到文本，确认无障碍服务已开启且当前在成交页");
    return;
  }
  const trade = parseTrade(raw);
  if (!trade) {
    toastLog("没解析出完整成交（需要方向/代码/数量/价格），原文：" + raw.slice(0, 120));
    return;
  }
  const key = signature(trade);
  if (alreadySynced(key)) {
    toastLog("这笔成交今天已经同步过：" + key);
    return;
  }

  const payload = {
    text: raw,
    account_id: trade.account_id,
    market: CONFIG.market,
    source: "notification",
    dry_run: false,
  };
  // 有结构化字段时也可以直接提交结构化，这里用原文让服务端再解析一遍，便于留痕
  http
    .postJson(CONFIG.api, payload, {
      headers: {
        "Content-Type": "application/json",
        Authorization: "Bearer " + CONFIG.token,
      },
      timeout: 30000,
    })
    .then(function (response) {
      const body = response.body ? JSON.parse(response.body.string()) : {};
      const data = body.data || body;
      if (data && data.ok) {
        markSynced(key);
        toastLog(data.duplicate ? "已同步过，跳过" : "已记录：" + key);
      } else {
        toastLog("同步失败：" + ((data && (data.reason || data.error)) || "未知错误"));
      }
    })
    .catch(function (error) {
      toastLog("请求失败：" + error);
    });
}

main();
