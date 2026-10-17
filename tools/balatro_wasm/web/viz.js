/* DecideX 可视化组件库(共享,零依赖,vanilla JS)
 *
 * 服务端 src/decidex/viz.py 产出 {"visual","tokens","lint","thresholds"} 这一份
 * 展示模型;本文件只负责把它画出来 —— 与 PlayJev 的分工一致:core 是模型,
 * web 是渲染。两个 web 工具(小丑牌 / 炉石)共用本文件,不再各写一份。
 *
 * 红线:这里只读不写。任何函数都不回传、不上报、不影响推荐结果。
 *
 * 全部走 textContent 建节点,与服务端渲染的 next 路径同一套 XSS 策略
 * (模型返回的 description / 选项 key 都算不可信输入)。
 */
(function (global) {
  "use strict";

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  }

  function clear(node) { while (node && node.firstChild) node.removeChild(node.firstChild); }

  const TIER_TEXT = { high: "高置信", medium: "需复核", low: "低置信" };

  /* 三段置信徽章:高/中/低。color 由服务端给(good/warn/bad),前端不重算阈值,
     避免两端各有一套数值后漂移。 */
  function confidenceBadge(conf) {
    if (!conf) return el("span", "vz-badge vz-bad", "无置信数据");
    const tier = conf.tier || "low";
    const wrap = el("span", "vz-badge vz-" + (conf.color || "bad"));
    if (conf.is_fallback) {
      wrap.appendChild(el("span", null, "回落"));
      wrap.title = "回落结果:非模型判断,置信度不具可比性";
      return wrap;
    }
    wrap.appendChild(el("span", null, TIER_TEXT[tier] || tier));
    wrap.appendChild(el("span", "vz-pct", (conf.percent !== undefined
      ? conf.percent.toFixed(0) : (conf.value * 100).toFixed(0)) + "%"));
    if (conf.advice) wrap.title = conf.advice;
    return wrap;
  }

  /* 横向概率条(PlayJev ProbBar):track + fill + 右侧百分比。
     ratio 用原概率;share 用截断后归一化的占比,保证 TOP-N 视图仍撑满。 */
  function probBarRow(bar, opts) {
    opts = opts || {};
    const row = el("div", "vz-bar" + (bar.winner ? " vz-win" : "")
      + (bar.selected ? " vz-sel" : ""));
    const label = el("div", "vz-bar-lbl", bar.label);
    label.title = bar.label;
    row.appendChild(label);

    const track = el("div", "vz-track");
    const fill = el("div", "vz-fill" + (bar.winner ? " vz-fill-win" : ""));
    fill.style.width = Math.max(0, Math.min(100, (bar.ratio || 0) * 100)) + "%";
    track.appendChild(fill);
    row.appendChild(track);

    row.appendChild(el("div", "vz-bar-pct", (bar.percent !== undefined
      ? bar.percent : (bar.ratio || 0) * 100).toFixed(1) + "%"));
    return row;
  }

  function probBars(bars, opts) {
    const box = el("div", "vz-bars");
    (bars || []).forEach(function (b) { box.appendChild(probBarRow(b, opts)); });
    return box;
  }

  /* CHOICE:按概率降序的选项条,胜出行高亮 + 合法动作说明。 */
  function choiceCard(v) {
    const box = el("div", "vz-card vz-choice");
    box.appendChild(el("div", "vz-kind", "choice · 选项分布"));
    box.appendChild(probBars(v.bars));
    const st = v.stats || {};
    box.appendChild(el("div", "vz-note", "选项 " + st.n_options
      + " · 领先优势 " + ((st.margin || 0) * 100).toFixed(1) + "%"
      + " · 分布熵 " + (st.entropy || 0).toFixed(2)
      + "(越接近 1 越犹豫)"));
    return box;
  }

  /* SCORE:加权等级在 0..n-1 刻度上的落点 + 每等级概率条。
     System One 返回的是加权等级索引,不是连续分值,所以画刻度不画量表。 */
  function scoreCard(v) {
    const box = el("div", "vz-card vz-score");
    box.appendChild(el("div", "vz-kind", "score · 等级落点"));
    const s = v.score || {};
    const n = s.n_levels || 0;
    if (s.marker !== null && s.marker !== undefined && n > 1) {
      const scale = el("div", "vz-ticks");
      for (let i = 0; i < n; i++) scale.appendChild(el("span", "vz-tick"));
      const pin = el("div", "vz-pin");
      pin.style.left = (s.marker * 100) + "%";
      scale.appendChild(pin);
      box.appendChild(scale);
      box.appendChild(el("div", "vz-note", "落点 "
        + (s.marker * 100).toFixed(0) + "% · 共 " + n + " 级"));
    }
    box.appendChild(probBars(v.bars));
    return box;
  }

  /* NOUL:P(yes) 大数字 + 0..1 三段条(强烈否 / 不确定 / 强烈是)。 */
  function noulCard(v) {
    const box = el("div", "vz-card vz-noul");
    box.appendChild(el("div", "vz-kind", "noul · 是/否"));
    const n = v.noul || {};
    const p = n.p_yes || 0;
    const big = el("div", "vz-pyes");
    big.appendChild(el("b", null, (p * 100).toFixed(1) + "%"));
    big.appendChild(el("span", "vz-note", " P(是)"));
    box.appendChild(big);

    const grad = el("div", "vz-grad");
    const pin = el("div", "vz-pin");
    pin.style.left = (p * 100) + "%";
    grad.appendChild(pin);
    box.appendChild(grad);

    const ZONE = { strong_no: "强烈否", uncertain: "不确定(±15%)", strong_yes: "强烈是" };
    box.appendChild(el("div", "vz-note", "判定:" + (ZONE[n.zone] || n.zone)
      + " · P(否) " + ((n.p_no || 0) * 100).toFixed(1) + "%"));
    return box;
  }

  function visualCard(report) {
    const v = report && report.visual;
    if (!v) return el("div", "vz-note", "无可视化数据");
    if (v.empty) return el("div", "vz-note", "模型未返回概率分布,无置信依据可画");
    if (v.kind === "score") return scoreCard(v);
    if (v.kind === "noul") return noulCard(v);
    return choiceCard(v);
  }

  /* Token 预算仪表:总预算 / 证据+最长问题 两条,谁先触顶谁标色。
     和 PlayJev 一样双预算 —— 单条超长问题会让整体预算看起来还很空。 */
  function tokenGauge(tokens) {
    const box = el("div", "vz-tokens");
    if (!tokens) return box;
    box.appendChild(el("div", "vz-kind", "Token 预算"));
    const b = tokens.binding || {};
    const pct = Math.min(100, (b.ratio || 0) * 100);
    const row = el("div", "vz-gauge vz-" + (b.level === "over" ? "bad"
      : b.level === "near" ? "warn" : "good"));
    const track = el("div", "vz-track");
    const fill = el("div", "vz-fill");
    fill.style.width = pct + "%";
    track.appendChild(fill);
    row.appendChild(track);
    row.appendChild(el("div", "vz-bar-pct", pct.toFixed(0) + "%"));
    box.appendChild(row);
    const KIND = { total: "总请求", state_plus_longest: "证据+最长问题" };
    box.appendChild(el("div", "vz-note",
      (KIND[b.kind] || b.kind) + " " + b.used + " / " + b.budget
      + " · 证据 " + tokens.state + " · 问题 " + tokens.questions
      + "(" + tokens.n_questions + " 条,最长 " + tokens.longest_question + ")"));
    return box;
  }

  /* 发前 lint 体检:错误 → 警告 → 提示,按严重度排序(服务端已排好)。 */
  function lintPanel(lint) {
    const box = el("div", "vz-lint");
    if (!lint) return box;
    const sum = lint.summary || {};
    box.appendChild(el("div", "vz-kind", "发前体检"
      + (sum.total ? "(" + sum.total + ")" : "")));
    const issues = lint.issues || [];
    if (!issues.length) {
      box.appendChild(el("div", "vz-note vz-good", "未发现问题"));
      return box;
    }
    const LV = { error: "错误", warning: "警告", info: "提示" };
    issues.forEach(function (it) {
      const row = el("div", "vz-issue vz-" + it.level);
      row.appendChild(el("span", "vz-lv", LV[it.level] || it.level));
      row.appendChild(el("span", "vz-code", it.code));
      row.appendChild(el("span", "vz-msg", it.message));
      box.appendChild(row);
    });
    return box;
  }

  /* 一次性渲染整块:徽章 + 卡片 + 体检 + token。调用方给一个容器。 */
  function renderViz(container, report) {
    if (!container) return;
    clear(container);
    if (!report) { container.appendChild(el("div", "vz-note", "无展示模型")); return; }
    container.appendChild(confidenceBadge(report.visual && report.visual.confidence));
    container.appendChild(visualCard(report));
    container.appendChild(lintPanel(report.lint));
    container.appendChild(tokenGauge(report.tokens));
  }

  global.DecidexViz = {
    el: el,
    clear: clear,
    confidenceBadge: confidenceBadge,
    probBars: probBars,
    visualCard: visualCard,
    tokenGauge: tokenGauge,
    lintPanel: lintPanel,
    render: renderViz,
  };
})(window);