// PanWatch 成交同步 —— AutoJs6 无障碍读取同花顺「成交」页
//
// 适用：你用的是同花顺手机端（实盘或模拟盘都行），但券商不发成交短信/推送。
// 原理：AutoJs6 打开无障碍服务后，读取当前页面所有 TextView 文本，
//       用正则拼出一笔成交，POST 到 PanWatch 的 /api/portfolio/trades/ingest。
//
// 测试顺序（建议照做，避免一上来就记错账）：
//   1) 手机浏览器先打开 http://电脑局域网IP:8085 能登录，说明网络通；
//   2) 在 PanWatch「理财 → 交易同步」里建一个 trade:write 令牌，记下账户 id；
//   3) 把下面 CONFIG.testText 填成一条示例成交，dryRun 保持 true，运行脚本，
//      看到「预览成功」说明令牌/网络/解析都通了；
//   4) 把 testText 清空，在同花顺里打开一笔「成交详情」页再运行，dryRun 仍为 true，
//      看控制台打印的原文和解析结果；
//   5) 确认无误后把 dryRun 改成 false（脚本会弹窗让你确认），正式记账；
//   6) 再运行一次，应提示「已同步过」，验证幂等。
//
// 注意：
//   - 手机上的 api 不能写 127.0.0.1，要写电脑的局域网 IP（如 http://192.168.1.10:8085），
//     并确保 Windows 防火墙允许 8085；
//   - 同花顺列表页可能同时显示多笔成交，脚本按第一条匹配记账。测试和日常都建议先点开
//     「成交详情」页再运行，避免记错；提交前会弹窗确认；
//   - 页面改版/字段缺失会解析失败并打印原文，不会乱记；
//   - 同一天重复运行靠本地 storages 去重，服务端还会按成交编号/复合键二次去重。

"auto";

const CONFIG = {
  // 电脑局域网 IP + 端口，例如 http://192.168.1.10:8085/api/portfolio/trades/ingest
  api: "http://192.168.1.10:8085/api/portfolio/trades/ingest",
  // 在 PanWatch「理财 → 交易同步」创建，scope 必须是 trade:write
  token: "pwmcp_你的同步令牌",
  // 「理财 → 交易同步」账户下拉对应的 id（curl 示例里的 account_id）
  accountId: 1,
  market: "CN",
  // 首次测试保持 true：只预览，不写入账本
  dryRun: true,
  // 非空时跳过读屏，用这段文字做自测；正式使用时清空
  testText: "【华泰证券】2026年11月11日10:31:05以29.50元买入002943宇晶股份200股，手续费5.00元",
};

const STORE_NAME = "panwatch_ths_sync";

function pad2(value) {
  var n = parseInt(value, 10) || 0;
  return (n < 10 ? "0" : "") + n;
}

function collectTexts() {
  var nodes = className("android.widget.TextView").find();
  var parts = [];
  for (var i = 0; i < nodes.size(); i++) {
    var text = nodes.get(i).text();
    if (text && text.trim()) parts.push(text.trim());
  }
  return parts.join(" | ");
}

function parseTrade(text) {
  var normalized = text
    .replace(/：/g, ":")
    .replace(/，/g, ",")
    .replace(/\s+/g, " ");

  var actionMatch = normalized.match(/(买入|卖出)/);
  // 不用后行断言，兼容 AutoJs 的 Rhino 正则
  var codeMatch = normalized.match(/(?:^|\D)(\d{6})(?!\d)/);
  var qtyMatch =
    normalized.match(/(?:成交数量|数量|成交股数)\s*:?\s*(\d+)/) ||
    normalized.match(/(\d+)\s*股/);
  var priceMatch =
    normalized.match(/(?:成交价格|成交均价|成交价|价格)\s*:?\s*([\d.]+)/) ||
    normalized.match(/@\s*([\d.]+)/) ||
    normalized.match(/以\s*([\d.]+)\s*元/);
  var dateMatch = normalized.match(/(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})/);
  var timeMatch = normalized.match(/(\d{1,2}:\d{2}(?::\d{2})?)/);
  var nameMatch =
    normalized.match(/(?:证券名称|股票名称|名称)\s*:?\s*([\u4e00-\u9fa5A-Za-z0-9*]{2,12})/) ||
    (codeMatch && normalized.match(new RegExp(codeMatch[1] + "\\s*([\\u4e00-\\u9fa5A-Za-z*]{2,10})")));
  var idMatch =
    normalized.match(/(?:成交编号|合同编号|流水号|委托编号)\s*:?\s*([A-Za-z0-9_-]{4,40})/);

  if (!actionMatch || !codeMatch || !qtyMatch || !priceMatch) return null;

  var tradeDate = "";
  if (dateMatch) {
    tradeDate = dateMatch[1] + "-" + pad2(dateMatch[2]) + "-" + pad2(dateMatch[3]);
  }
  return {
    action: actionMatch[1] === "买入" ? "buy" : "sell",
    symbol: codeMatch[1],
    name: nameMatch ? nameMatch[1] : "",
    quantity: parseInt(qtyMatch[1], 10),
    price: parseFloat(priceMatch[1]),
    tradeDate: tradeDate,
    time: timeMatch ? timeMatch[1] : "",
    externalId: idMatch ? idMatch[1] : "",
  };
}

function signature(trade) {
  return [trade.tradeDate, trade.symbol, trade.action, trade.quantity, trade.price].join("|");
}

function alreadySynced(key) {
  return !!storages.create(STORE_NAME).get(key);
}

function markSynced(key) {
  storages.create(STORE_NAME).put(key, Date.now());
}

function summarize(trade) {
  return (
    (trade.action === "buy" ? "买入 " : "卖出 ") +
    (trade.name ? trade.name + " " : "") +
    trade.symbol +
    " " +
    trade.quantity +
    " 股 @ " +
    trade.price +
    (trade.tradeDate ? "（" + trade.tradeDate + (trade.time ? " " + trade.time : "") + "）" : "")
  );
}

function main() {
  console.show();
  console.log("=== PanWatch 交易同步 ===");

  var raw = CONFIG.testText && CONFIG.testText.trim() ? CONFIG.testText.trim() : collectTexts();
  if (!raw) {
    console.log("没读到页面文本：确认无障碍服务已开启，且当前在同花顺成交页");
    toastLog("没读到页面文本，检查无障碍服务");
    return;
  }
  console.log("读到原文：" + raw.substring(0, 300));

  var trade = parseTrade(raw);
  if (!trade) {
    console.log("解析失败：需要方向/代码/数量/价格，原文见上");
    toastLog("没解析出完整成交，原文已打印到控制台");
    return;
  }
  console.log("解析结果：" + summarize(trade));
  var key = signature(trade);
  if (alreadySynced(key)) {
    console.log("本地已同步过：" + key);
    toastLog("这笔成交今天已经同步过");
    return;
  }

  var payload = {
    action: trade.action,
    symbol: trade.symbol,
    name: trade.name || "",
    quantity: trade.quantity,
    price: trade.price,
    trade_date: trade.tradeDate || null,
    time: trade.time || "",
    external_id: trade.externalId || "",
    note: raw.substring(0, 500),
    source: "notification",
    market: CONFIG.market,
    account_id: CONFIG.accountId,
    dry_run: CONFIG.dryRun,
  };

  if (!CONFIG.dryRun) {
    if (!confirm(summarize(trade) + "\n\n确认记入 PanWatch？", "交易同步")) {
      console.log("用户取消");
      return;
    }
  }

  try {
    var response = http.postJson(CONFIG.api, payload, {
      headers: { Authorization: "Bearer " + CONFIG.token },
      timeout: 30000,
    });
    var body = JSON.parse(response.body.string());
    var data = body.data || body;
    if (data && data.ok) {
      if (data.duplicate) {
        markSynced(key);
        console.log("服务端已有这笔，跳过");
        toastLog("已同步过，跳过");
      } else if (data.dry_run) {
        console.log("预览成功（未记账）：" + JSON.stringify(data.payload || payload));
        toastLog("预览成功：" + summarize(trade) + "（未记账）");
      } else {
        markSynced(key);
        console.log("已记录：" + key);
        toastLog("已记录：" + summarize(trade));
      }
    } else {
      console.log("同步失败：" + JSON.stringify(data));
      toastLog("同步失败：" + ((data && (data.reason || data.error)) || "未知错误"));
    }
  } catch (error) {
    console.log("请求失败：" + error);
    toastLog("请求失败，检查 api 地址/网络/令牌");
  }
}

main();
