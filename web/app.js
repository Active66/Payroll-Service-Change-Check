/* ---------------------------------------------------------------------------
 * 薪酬变更核对 · 制度问答（原生 JS，零依赖，零构建）
 *
 * 三件事是这文件的重点，其余都是 DOM 拼装：
 *   ① SSE 手工解析：EventSource 只支持 GET 且不能带 body，所以必须 fetch + ReadableStream；
 *   ② 二级缓冲打字机：网络到达速度与阅读速度解耦，否则高 TPS 模型会"闪一下全出来"；
 *   ③ 中文输入法护栏：Enter 在拼字过程中是"上屏"，绝不能当成发送。
 *
 * 安全约定：**用户与模型的文本一律走 textContent**，只有固定的骨架用 DOM 拼，
 * 全文件没有一处 innerHTML 拼接动态内容。
 * ------------------------------------------------------------------------- */

"use strict";

/* ============================ 状态 ============================ */

/**
 * 401 统一处理：登录过期时把用户送回登录页，并带上当前地址，
 * 登录后能回到原位。不这么做的话，用户只会看到"请求失败，请稍后重试"，
 * 然后一直重试一直失败 —— 这在接入鉴权之后是最常见的求助原因。
 */
function redirectIfUnauthorized(resp) {
  if (resp.status !== 401) return false;
  const next = encodeURIComponent(location.pathname + location.search);
  location.href = `/login?next=${next}`;
  return true;
}

const S = {
  sessionId: localStorage.getItem("payroll.session") || null,
  streaming: false,
  controller: null,
  lastQuestion: "",
};

const els = {
  thread: document.getElementById("thread"),
  welcome: document.getElementById("welcome"),
  form: document.getElementById("form"),
  input: document.getElementById("input"),
  send: document.getElementById("send"),
  statusbar: document.getElementById("statusbar"),
  statusText: document.getElementById("statusText"),
  health: document.getElementById("health"),
  healthText: document.getElementById("healthText"),
  jump: document.getElementById("jump"),
  newChat: document.getElementById("newChat"),
  mic: document.getElementById("mic"),
};

const STATUS_LABEL = {
  answered: "已核对",
  partial: "待核",
  refused: "未找到依据",
  error: "出错",
  streaming: "生成中",
  drafted: "已建草稿",
  escalated: "已转人工",
  degraded: "已降级",
  inconsistent: "结果待复核",
  awaiting_confirmation: "草稿待确认",
};

// Agent 工具名 → 状态条文案。没列到的工具显示原名。
const TOOL_LABEL = {
  lookup_policy: "正在检索制度条款…",
  lookup_param: "正在核对制度参数…",
  get_employee: "正在读取员工档案…",
  get_change_order: "正在查询历史变更单…",
  check_rules: "正在跑规则校验…",
  create_draft: "正在起草变更单…",
  submit_draft: "正在提交…",
  escalate_to_human: "正在转人工…",
  ask_clarification: "正在向您确认…",
};

/* ============================ 小工具 ============================ */

/** 造元素。文本一律 textContent —— 这是整套 XSS 防线的唯一入口。 */
function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

function nearBottom() {
  const t = els.thread;
  return t.scrollHeight - t.scrollTop - t.clientHeight < 90;
}

/** 只在用户本来就贴着底部时才跟随滚动，避免"我往上翻历史，它把我拽回来"。 */
function followScroll(force) {
  if (force || nearBottom()) els.thread.scrollTop = els.thread.scrollHeight;
}

function hideWelcome() {
  if (els.welcome && els.welcome.parentNode) els.welcome.remove();
}

function setStatus(text, visible) {
  if (text) els.statusText.textContent = text;
  els.statusbar.hidden = !visible;
}

function setStreaming(on) {
  S.streaming = on;
  els.send.textContent = on ? "停止" : "发送";
  els.send.classList.toggle("stop", on);
  els.input.disabled = false;           // 允许边生成边打字下一条
  // 语音按钮跟随流式状态：识别结果会覆盖正在编辑的输入，生成中禁用最省事
  document.dispatchEvent(new CustomEvent("pcc:streaming", { detail: on }));
}

/* ============================ 消息渲染 ============================ */

function addUserMessage(text) {
  hideWelcome();
  const wrap = el("article", "msg user");
  wrap.appendChild(el("div", "bubble", text));
  els.thread.appendChild(wrap);
  followScroll(true);
  return wrap;
}

/**
 * 创建一条"助手气泡"的骨架，返回各部件引用。
 * 结构：头像 + [气泡(正文+光标+护栏条)] + [引用来源] + [状态徽标]
 */
function addBotMessage() {
  hideWelcome();
  const wrap = el("article", "msg bot");
  const col = el("div", "col");

  const bubble = el("div", "bubble");
  const text = el("span", "text");
  const caret = el("span", "caret");
  const guard = el("div", "guard");
  guard.hidden = true;
  bubble.append(text, caret, guard);

  const sources = el("div", "sources");
  sources.hidden = true;
  const meta = el("div", "meta");

  col.append(bubble, sources, meta);
  wrap.append(el("div", "avatar", "制"), col);
  els.thread.appendChild(wrap);
  followScroll(true);

  return { wrap, bubble, text, caret, guard, sources, meta };
}

/**
 * 打字机：网络层 push() 文本，rAF 循环按固定速率"吐"到 DOM。
 *
 * 为什么要二级缓冲：
 *   mock 模型或高 TPS 模型会在一帧内送来几十个字，直接 append 等于"瞬间全出"。
 *   缓冲后按 42 字/秒渲染（接近舒适阅读速度），收尾时提到 480 字/秒追平，
 *   用户既能看到"在生成"，又不会等太久。
 *
 * 为什么用 requestAnimationFrame 而不是 setInterval：
 *   后台标签页里 rAF 会自动暂停，不会出现"切回来瞬间闪出几百字"。
 */
function createTypewriter(textEl) {
  let queue = "";
  let shown = "";
  let finished = false;
  let raf = 0;
  let prev = 0;
  let onSettle = null;

  function tick(ts) {
    raf = 0;
    const dt = prev ? Math.min(ts - prev, 80) : 16;   // 掉帧时限制单帧补字量
    prev = ts;
    const cps = finished ? 480 : 42;
    const take = Math.max(1, Math.round((cps * dt) / 1000));

    if (queue) {
      shown += queue.slice(0, take);
      queue = queue.slice(take);
      textEl.textContent = shown;
      followScroll();
    }

    if (queue || !finished) {
      raf = requestAnimationFrame(tick);
    } else {
      prev = 0;
      if (onSettle) {
        const fn = onSettle;
        onSettle = null;
        fn();
      }
    }
  }

  return {
    push(chunk) {
      if (!chunk) return;
      queue += chunk;
      if (!raf) raf = requestAnimationFrame(tick);
    },
    end() {
      finished = true;
      if (!raf) raf = requestAnimationFrame(tick);
    },
    get text() {
      return shown + queue;
    },
    /** 打字追平后回调。用于"打完了再上角标"——流式期间上角标会随文本抖动。 */
    settle(fn) {
      if (finished && !queue) fn();
      else onSettle = fn;
    },
  };
}

/**
 * 把正文里的 [1] [2] 变成可点击上标。
 *
 * 只在整段收尾后调用一次：流式过程中如果边打边插 <sup>，
 * 半截出现的 "[1" 会被拆成文本节点，标点一到位又要重排，视觉上会跳。
 */
function paintWithCites(textEl, text, maxIndex) {
  textEl.textContent = "";
  const re = /\[(\d{1,2})\]/g;
  let last = 0;
  let m;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) textEl.appendChild(document.createTextNode(text.slice(last, m.index)));
    const n = Number(m[1]);
    if (n >= 1 && n <= maxIndex) {
      const sup = el("sup", "cite", String(n));
      sup.dataset.index = String(n);
      textEl.appendChild(sup);
    } else {
      // 越界角标原样显示：它本身就是护栏要提示的异常，不该被悄悄吃掉
      textEl.appendChild(document.createTextNode(m[0]));
    }
    last = m.index + m[0].length;
  }
  textEl.appendChild(document.createTextNode(text.slice(last)));
}

/** 引用来源卡片：文件名 + 条款号 + 120 字片段。全部 textContent。 */
function renderCitations(box, citations) {
  box.textContent = "";
  if (!citations || !citations.length) {
    box.hidden = true;
    return;
  }
  box.appendChild(el("div", "sources-title", `引用来源（${citations.length} 条）`));

  const list = el("ol", "sources-list");
  for (const c of citations) {
    const li = el("li", "source");
    li.dataset.index = String(c.index);

    const head = el("div", "source-head");
    head.appendChild(el("span", "source-num", String(c.index)));
    head.appendChild(el("span", "source-name", `《${c.doc_title || c.doc_id}》${c.clause_no || ""}`));
    if (c.source_file) head.appendChild(el("span", "source-file", c.source_file));

    li.appendChild(head);
    li.appendChild(el("p", "source-snippet", c.snippet || ""));
    list.appendChild(li);
  }
  box.appendChild(list);
  box.hidden = false;
}

/** 点角标 → 高亮对应卡片并滚到它。 */
function flashSource(scope, index) {
  const card = scope.querySelector(`.source[data-index="${index}"]`);
  if (!card) return;
  card.classList.add("flash");
  card.scrollIntoView({ block: "nearest", behavior: "smooth" });
  setTimeout(() => card.classList.remove("flash"), 1600);
}

function renderDraftConfirm(ui, draft) {
  /**
   * 草稿确认卡片。Agent 决定建单时，循环会强制中断，草稿流式到这里
   * 让员工过目；点"确认建单"才真正调用后端落库。不点，单不存在。
   */
  const card = el("div", "draft-confirm");
  card.appendChild(el("div", "draft-title", "草稿待确认 · 请核对以下内容"));

  const info = el("div", "draft-body");
  const rows = [
    ["员工", (draft.employee_name || draft.employee_no || "—")],
    ["变更类型", draft.change_type || "—"],
  ];
  for (const it of (draft.items || [])) {
    const eff = it.effective_date ? `（${it.effective_date} 生效）` : "";
    rows.push(["明细", `${it.field}: ${it.old_value || "—"} → ${it.new_value || "—"}${eff}`]);
  }
  rows.push(["风险等级", draft.risk_level || "—"]);
  if ((draft.materials || []).length) {
    rows.push(["材料", draft.materials.join("、")]);
  }
  for (const [k, v] of rows) {
    const row = el("div", "draft-row");
    row.appendChild(el("span", "draft-k", k));
    row.appendChild(el("span", "draft-v", v));
    info.appendChild(row);
  }
  if (draft.warnings && draft.warnings.length) {
    info.appendChild(el("div", "draft-warn",
      (draft.warnings.map(w => `[${w.code}] ${w.message}`)).join("；")));
  }
  card.appendChild(info);

  const btns = el("div", "draft-actions");
  const ok = el("button", "draft-ok", "确认建单");
  const cancel = el("button", "draft-cancel", "取消");
  ok.type = "button";
  cancel.type = "button";
  btns.append(ok, cancel);
  card.appendChild(btns);

  // 插到引用卡片之前，紧跟答案气泡
  ui.wrap.insertBefore(card, ui.sources);
  followScroll(true);

  ok.addEventListener("click", async () => {
    ok.disabled = true;
    ok.textContent = "正在建单…";
    try {
      const resp = await fetch("/api/agent/confirm-draft", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        // idempotency_key 用草稿内容哈希：连点两次也只建一张单
        body: JSON.stringify({
          validation_token: draft.validation_token,
          change_type: draft.change_type,
          employee_no: draft.employee_no,
          items: draft.items,
          materials: draft.materials,
          idempotency_key: draft.draft_hash,
          session_id: S.sessionId,
        }),
      });
      if (!resp.ok) {
        const body = await resp.json().catch(() => null);
        const err = (body && body.error) || { message: "建单失败" };
        throw Object.assign(new Error(err.message), { apiError: err });
      }
      const data = (await resp.json()).data;
      ok.textContent = "已建单";
      cancel.hidden = true;
      card.appendChild(el("div", "draft-done",
        `已建单：${data.order_no}（风险 ${data.risk_level || "—"}），请等待审批。`));
    } catch (ex) {
      ok.disabled = false;
      ok.textContent = "重试";
      card.appendChild(el("div", "draft-err", ex.message || "建单失败，请重试"));
    }
  });

  cancel.addEventListener("click", () => {
    ok.disabled = true;
    cancel.disabled = true;
    card.appendChild(el("div", "draft-done", "已取消，未建单。可重新发起，或说明需要修改的地方。"));
  });
}

function renderGuard(box, data) {
  box.textContent = "";
  const title = el("div", "guard-title",
    data.status === "partial" ? "需要留意：以下内容未能完全对上原文" : "需要留意");
  box.appendChild(title);
  if (data.warnings && data.warnings.length) {
    const ul = el("ul");
    for (const w of data.warnings) ul.appendChild(el("li", null, w));
    box.appendChild(ul);
  }
  box.hidden = false;
}

function renderError(bubble, err) {
  const box = el("div", "err");
  box.appendChild(el("p", null, err.message || "请求失败，请稍后重试"));
  if (err.code) {
    box.appendChild(el("span", "err-meta",
      `${err.code}${err.trace_id ? " · " + err.trace_id : ""}`));
  }
  const retry = el("button", "retry", "重试");
  retry.type = "button";
  retry.addEventListener("click", () => {
    if (!S.lastQuestion) return;
    box.remove();
    ask(S.lastQuestion);
  });
  box.appendChild(retry);
  bubble.appendChild(box);
  followScroll();
}

function renderMeta(metaEl, status, usage) {
  metaEl.textContent = "";
  const badge = el("span", `badge ${status || ""}`, STATUS_LABEL[status] || status || "");
  metaEl.appendChild(badge);
  if (usage && usage.latency_ms != null) {
    metaEl.appendChild(el("span", null, `耗时 ${(usage.latency_ms / 1000).toFixed(1)}s`));
  }
  if (usage && usage.tokens_out) {
    metaEl.appendChild(el("span", null, `输出 ${usage.tokens_out} tokens`));
  }
}

/* ============================ SSE 解析 ============================ */

/**
 * 从 fetch 的 ReadableStream 里读 SSE。
 *
 * 三个必须处理的细节（漏一个就会出现"偶发丢字"或"偶发解析失败"）：
 *   ① **chunk 边界会切断一帧**：只处理以空行结尾的完整块，剩下的半截留在 buffer；
 *   ② 一帧可能有**多行 data:**，按规范要用换行拼起来；
 *   ③ 以 `:` 开头是注释（服务端心跳），必须跳过，不能当 JSON 解。
 */
async function readSSE(response, handlers) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";

  const dispatch = (block) => {
    let event = "message";
    const dataLines = [];
    for (const raw of block.split("\n")) {
      const line = raw.endsWith("\r") ? raw.slice(0, -1) : raw;
      if (!line || line.startsWith(":")) continue;
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) dataLines.push(line.slice(5).replace(/^ /, ""));
    }
    if (!dataLines.length) return;
    let payload;
    try {
      payload = JSON.parse(dataLines.join("\n"));
    } catch {
      return;                                   // 脏帧直接丢弃，不影响后续
    }
    const fn = handlers[event];
    if (fn) fn(payload);
  };

  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let idx;
    while ((idx = buffer.indexOf("\n\n")) >= 0) {
      dispatch(buffer.slice(0, idx));
      buffer = buffer.slice(idx + 2);
    }
  }
  // 少数服务端最后一帧不带空行，收尾时补一次
  if (buffer.trim()) dispatch(buffer);
}

/* ============================ 主流程 ============================ */

async function ask(question) {
  if (S.streaming) return;
  question = (question || "").trim();
  if (!question) return;

  S.lastQuestion = question;
  addUserMessage(question);
  els.input.value = "";
  autosize();

  const ui = addBotMessage();
  const tw = createTypewriter(ui.text);
  let citations = [];
  let settled = false;

  const finishOnce = (status, usage) => {
    if (settled) return;
    settled = true;
    ui.wrap.classList.add("done");
    if (ui.caret.parentNode) ui.caret.remove();
    // 打完之后再上角标：流式期间保持纯文本
    tw.settle(() => {
      if (status !== "error" && citations.length) {
        paintWithCites(ui.text, tw.text, citations.length);
      }
    });
    renderMeta(ui.meta, status, usage);
  };

  setStreaming(true);
  setStatus("正在检索制度…", true);
  S.controller = new AbortController();

  let sawTerminal = false;

  try {
    const resp = await fetch("/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json", "Accept": "text/event-stream" },
      body: JSON.stringify({ session_id: S.sessionId, question }),
      signal: S.controller.signal,
    });

    // 流开始之前的失败：HTTP 状态码 + JSON 错误体（与流中错误同结构）
    if (redirectIfUnauthorized(resp)) return;
    if (!resp.ok) {
      const body = await resp.json().catch(() => null);
      const err = (body && body.error) || {
        code: `HTTP_${resp.status}`,
        message: "请求失败，请稍后重试",
      };
      if (err.code === "SES_001" || err.code === "REQ_005") {
        // 会话过期/非法：清掉本地记录，用户重试时会自动新建
        S.sessionId = null;
        localStorage.removeItem("payroll.session");
      }
      throw Object.assign(new Error(err.message), { apiError: err });
    }

    await readSSE(resp, {
      session(d) {
        S.sessionId = d.session_id;
        localStorage.setItem("payroll.session", d.session_id);
      },
      status(d) {
        if (d.stage === "generating") setStatus("正在组织回答…", true);
        else if (d.stage === "refused") setStatus(d.text || "没有检索到相关制度条款", true);
        else setStatus(d.text || "正在检索制度…", true);
      },
      citations(d) {
        citations = d.citations || [];
        renderCitations(ui.sources, citations);
        ui.wrap.citations = citations;
      },
      delta(d) {
        // 首字到达即收起状态条，换成光标
        if (d.text) setStatus("", false);
        tw.push(d.text || "");
      },
      tool_call(d) {
        // Agent 模式：把"正在调什么"显示在状态条上，让等待过程可见
        const label = TOOL_LABEL[d.name] || d.name || "正在处理…";
        setStatus(label, true);
      },
      tool_result(d) {
        if (!d.ok && d.error_code && !d.error_code.startsWith("AGT_009")) {
          // 工具被拒绝：状态条提示一下，但不打断最终答案。
          // AGT_009（提交需确认）是正常流程，不该显示成错误
          setStatus("提示：" + (d.summary || "该操作未通过校验"), true);
        }
      },
      guard(d) {
        renderGuard(ui.guard, d);
      },
      done(d) {
        sawTerminal = true;
        tw.end();
        // Agent 模式 status 可能是 drafted / escalated / inconsistent，
        // finishOnce 里 renderMeta 会把它原样显示
        finishOnce(d.status || "answered", d.usage);
        // Agent 被拦截在"建单前"：气泡收尾后再弹确认卡片
        if (d.status === "awaiting_confirmation" && d.draft_preview) {
          renderDraftConfirm(ui, d.draft_preview);
        }
      },
      error(d) {
        sawTerminal = true;
        tw.end();
        renderError(ui.bubble, d);
        finishOnce("error", null);
      },
    });

    if (!sawTerminal) {
      // 连接被对端关掉但没有任何终止事件：按中断处理，别让气泡永远转圈
      tw.end();
      renderError(ui.bubble, { code: "CLI_001", message: "连接已中断，请点击重试" });
      finishOnce("error", null);
    }
  } catch (ex) {
    tw.end();
    if (ex.name === "AbortError") {
      renderMeta(ui.meta, "error", null);
      ui.meta.appendChild(el("span", null, "已停止生成"));
      finishOnce("error", null);
    } else {
      const err = ex.apiError || {
        code: "NET",
        message: "无法连接服务，请确认后端已启动（python -m my_tool serve）",
      };
      renderError(ui.bubble, err);
      finishOnce("error", null);
    }
  } finally {
    setStatus("", false);
    setStreaming(false);
    S.controller = null;
  }
}

/* ============================ 输入区 ============================ */

function autosize() {
  els.input.style.height = "auto";
  els.input.style.height = `${Math.min(els.input.scrollHeight, 168)}px`;
}

function submit() {
  if (S.streaming) {
    // 发送键在生成中就是"停止"：中断上一条，服务端随之停止生成（省 token）
    if (S.controller) S.controller.abort();
    return;
  }
  ask(els.input.value);
}

els.form.addEventListener("submit", (e) => {
  e.preventDefault();
  submit();
});

els.input.addEventListener("input", autosize);

els.input.addEventListener("keydown", (e) => {
  if (e.key !== "Enter" || e.shiftKey) return;
  // 中文输入法的关键判断：拼字中按 Enter 是"上屏候选词"，不是发送。
  // isComposing 在 Chrome/Safari 可用，keyCode 229 兜住老浏览器与部分 Linux 输入法。
  if (e.isComposing || e.keyCode === 229) return;
  e.preventDefault();
  submit();
});

/* 角标点击 → 高亮来源卡片 */
els.thread.addEventListener("click", (e) => {
  const cite = e.target.closest(".cite");
  if (!cite) return;
  const scope = cite.closest(".msg");
  if (scope) flashSource(scope, cite.dataset.index);
});

/* 回到最新 */
els.thread.addEventListener("scroll", () => {
  els.jump.hidden = nearBottom();
});
els.jump.addEventListener("click", () => followScroll(true));

/* 示例问题 */
document.querySelectorAll(".samples button").forEach((btn) => {
  btn.addEventListener("click", () => ask(btn.textContent));
});

/* ============================ 语音输入 ============================
 *
 * 用浏览器自带的 Web Speech API：音频不出本机、服务端零改动、零成本。
 *
 * 三个刻意的决定：
 *   ① **只填进输入框，不自动发送**。识别错一个字，检索就可能错一条条款；
 *      必须给用户"看一眼再按回车"的机会。语音是输入方式，不是提交方式。
 *   ② 识别中允许继续追加：连续说几句，按说话顺序拼起来，而不是覆盖。
 *   ③ 不支持的浏览器（Firefox / Safari 部分版本）直接隐藏按钮，
 *      而不是留一个点了没反应的按钮——那比没有更让人困惑。
 */
const SpeechRec =
  window.SpeechRecognition || window.webkitSpeechRecognition || null;

let recog = null;
let micText = "";          // 本次识别累计的文本，final 结果逐句追加

function micSupported() {
  return !!SpeechRec;
}

function startMic() {
  if (!recog) {
    recog = new SpeechRec();
    // 中文普通话：本系统的制度是中文的，识别语言跟错会让专有名词全崩
    recog.lang = "zh-CN";
    recog.interimResults = true;   // 边说边显示，等待感小得多
    recog.continuous = false;      // 说一句自动停，避免忘记关麦克风

    let interim = "";
    recog.onresult = (ev) => {
      interim = "";
      for (let i = ev.resultIndex; i < ev.results.length; i++) {
        const r = ev.results[i];
        if (r.isFinal) {
          micText += r[0].transcript;
        } else {
          interim += r[0].transcript;
        }
      }
      els.input.value = (micText + interim).trim();
      autosize();
    };
    recog.onerror = (ev) => {
      stopMic();
      // not-allowed = 用户拒了麦克风权限；no-speech = 没说话。
      // 都只提示一句，不弹窗——这是输入辅助，不该打断对话
      if (ev.error === "not-allowed" || ev.error === "service-not-allowed") {
        setStatus("麦克风权限被拒绝，请在浏览器地址栏左侧重新允许", true);
      } else if (ev.error === "no-speech") {
        setStatus("没有听清，请再说一次", true);
      }
    };
    recog.onend = () => stopMic();
  }
  micText = els.input.value ? els.input.value.replace(/\s*$/, " ") : "";
  try {
    recog.start();
    els.mic.classList.add("recording");
    els.mic.textContent = "■";
    setStatus("正在听…（说一句后自动停止）", false);
  } catch (ex) {
    // 连点两次会抛 InvalidStateError，忽略即可
    stopMic();
  }
}

function stopMic() {
  if (recog) {
    try { recog.stop(); } catch (ex) { /* 已停止 */ }
  }
  els.mic.classList.remove("recording");
  els.mic.textContent = "🎙";
  setStatus("", false);
  els.input.focus();
}

if (els.mic && micSupported()) {
  els.mic.hidden = false;
  els.mic.addEventListener("click", () => {
    if (els.mic.classList.contains("recording")) stopMic();
    else startMic();
  });
  // 生成中不允许开麦克风：识别文本会覆盖用户正在编辑的下一条问题，
  // 表现为"我刚打的字不见了"。状态在 setStreaming 里同步。
  document.addEventListener("pcc:streaming", (e) => {
    els.mic.disabled = !!e.detail;
    els.mic.title = e.detail ? "生成回答时暂不能语音输入"
                             : "语音输入（识别后先填进输入框，确认再发送）";
    if (e.detail && els.mic.classList.contains("recording")) stopMic();
  });
}

/* 新建对话：清本地会话，清空消息区 */
els.newChat.addEventListener("click", () => {
  if (S.controller) S.controller.abort();
  S.sessionId = null;
  localStorage.removeItem("payroll.session");
  els.thread.textContent = "";
  const welcome = document.createElement("section");
  welcome.className = "welcome";
  welcome.id = "welcome";
  const h2 = el("h2", null, "问点什么？");
  const p = el("p", null, "只会依据《员工薪酬变更核对制度 v1.0》回答，每条结论都带条款出处。");
  const box = el("div", "samples");
  for (const q of [
    "冻结期内能改银行账户吗？",
    "2026 年上海社保缴费基数上限是多少？",
    "单次补发最多覆盖几个月？",
    "降薪需要员工书面确认吗？",
  ]) {
    const b = el("button", null, q);
    b.type = "button";
    b.addEventListener("click", () => ask(q));
    box.appendChild(b);
  }
  welcome.append(h2, p, box);
  els.thread.appendChild(welcome);
  els.welcome = welcome;
  els.input.focus();
});

/* ============================ 启动 ============================ */

async function loadHealth() {
  try {
    const resp = await fetch("/healthz");
    const body = await resp.json();
    const d = body.data || {};
    const ready = !!(d.index && d.index.ready);
    els.health.className = "health " + (ready ? "ok" : "bad");
    els.healthText.textContent = ready
      ? `索引就绪 · ${d.index.chunk_count ?? "?"} 条切片`
      : "索引未就绪";
    els.health.title = ready
      ? `模型 ${d.llm && d.llm.model}｜文件 ${d.index.source_file || "-"}`
      : (d.index && d.index.error) || "请先执行 build";
  } catch {
    els.health.className = "health bad";
    els.healthText.textContent = "服务不可用";
  }
}

/** 刷新后恢复历史：没有这一步，"最近 6 轮"用户根本感知不到。 */
async function restoreHistory() {
  if (!S.sessionId) return;
  try {
    const resp = await fetch(`/sessions/${encodeURIComponent(S.sessionId)}/messages?limit=50`);
    if (redirectIfUnauthorized(resp)) return;
    if (!resp.ok) {
      S.sessionId = null;
      localStorage.removeItem("payroll.session");
      return;
    }
    const body = await resp.json();
    const messages = (body.data && body.data.messages) || [];
    if (!messages.length) return;

    for (const m of messages) {
      if (m.role === "user") {
        addUserMessage(m.content);
        continue;
      }
      const ui = addBotMessage();
      ui.wrap.classList.add("done");
      if (ui.caret.parentNode) ui.caret.remove();
      ui.text.textContent = m.content || "（无内容）";
      const cites = m.citations || [];
      if (cites.length) {
        renderCitations(ui.sources, cites);
        paintWithCites(ui.text, m.content || "", cites.length);
      }
      if (m.answer_status === "streaming") {
        // 上次没跑完：明确告诉用户，而不是留个空白气泡
        ui.text.textContent = (m.content || "") + "（这条回答中断了）";
      }
      renderMeta(ui.meta, m.answer_status || "answered", null);
    }
    followScroll(true);
  } catch {
    /* 恢复失败不阻塞新提问 */
  }
}

els.input.focus();
autosize();
loadHealth();
restoreHistory();
