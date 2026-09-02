/*
Smart Meta WhatsApp Commands
============================

Flexible Arabic + English command parser.

Examples:
- 7day EK Age
- 7day - Esraa - Age
- Today AA
- Allocation
- عايز تقرير لعبدالله انهاردة للصرف وال AGE
- عايز المحافظات لاسراء اخر 7 ايام
*/

const MENU_TRIGGERS = new Set([
  "hi",
  "hello",
  "report",
  "menu",
  "help",
  "start",
  "ابدا",
  "ابدأ",
  "تقرير",
  "القائمه",
  "القائمة",
]);

const RANGE_BY_NUMBER = {
  "1": { key: "today", label: "Today" },
  "2": { key: "yesterday", label: "Yesterday" },
  "3": { key: "7d", label: "Last 7 Days" },
  "4": { key: "30d", label: "Last 30 Days" },
  "5": { key: "this_month", label: "This Month" },
  "6": { key: "last_month", label: "Last Month" },
};

const RANGE_ALIASES = [
  {
    key: "last_month",
    label: "Last Month",
    aliases: [
      "last month",
      "previous month",
      "الشهر اللي فات",
      "الشهر الماضي",
      "الشهر السابق",
    ],
  },
  {
    key: "this_month",
    label: "This Month",
    aliases: [
      "this month",
      "current month",
      "الشهر ده",
      "الشهر دا",
      "الشهر الحالي",
    ],
  },
  {
    key: "30d",
    label: "Last 30 Days",
    aliases: [
      "30d",
      "30day",
      "30days",
      "30 day",
      "30 days",
      "last 30 days",
      "اخر 30 يوم",
      "اخر 30 ايام",
    ],
  },
  {
    key: "7d",
    label: "Last 7 Days",
    aliases: [
      "7d",
      "7day",
      "7days",
      "7 day",
      "7 days",
      "last 7 days",
      "اخر 7 يوم",
      "اخر 7 ايام",
      "اسبوع",
      "الاسبوع",
      "اخر اسبوع",
    ],
  },
  {
    key: "yesterday",
    label: "Yesterday",
    aliases: [
      "yesterday",
      "امبارح",
      "الامس",
      "امس",
    ],
  },
  {
    key: "today",
    label: "Today",
    aliases: [
      "today",
      "النهارده",
      "النهاردة",
      "انهارده",
      "انهاردة",
      "اليوم",
    ],
  },
];

const AGENTS = {
  AA: {
    name: "Abdallah Adel",
    aliases: [
      "aa",
      "abdallah",
      "abdullah",
      "abdallah adel",
      "abdullah adel",
      "عبدالله",
      "عبد الله",
      "عبدالله عادل",
      "عبد الله عادل",
    ],
  },
  HM: {
    name: "Ahmed Hesham",
    aliases: [
      "hm",
      "ahmed hesham",
      "ahmed hisham",
      "احمد هشام",
    ],
  },
  BM: {
    name: "Bassem Shalawy",
    aliases: [
      "bm",
      "bassem",
      "bassem shalawy",
      "باسم",
      "باسم شلاوي",
    ],
  },
  EK: {
    name: "Esraa Kamal",
    aliases: [
      "ek",
      "esraa",
      "israa",
      "esraa kamal",
      "israa kamal",
      "اسراء",
      "اسراء كمال",
    ],
  },
  MA: {
    name: "Mahmoud",
    aliases: [
      "ma",
      "mahmoud",
      "محمود",
    ],
  },
  AF: {
    name: "Amr Fathy",
    aliases: [
      "af",
      "amr",
      "amr fathy",
      "عمرو",
      "عمرو فتحي",
    ],
  },
  SQ: {
    name: "(R)Ahmed Sharkawy",
    aliases: [
      "sq",
      "ahmed sharkawy",
      "sharkawy",
      "احمد الشرقاوي",
      "الشرقاوي",
    ],
  },
  OS: {
    name: "(R)Osama Serwe",
    aliases: [
      "os",
      "osama",
      "osama serwe",
      "اسامه",
      "اسامة",
    ],
  },
  MM: {
    name: "(R)Mohamed Mahmoud",
    aliases: [
      "mm",
      "mohamed mahmoud",
      "محمد محمود",
    ],
  },
  NB: {
    name: "(R)Mohamed Nabih",
    aliases: [
      "nb",
      "mohamed nabih",
      "mohamed nabeih",
      "محمد نبيه",
    ],
  },
};


const TEAMS = {
  taher: {
    label: "Taher Team",
    aliases: [
      "taher",
      "taher team",
      "team taher",
      "طاهر",
      "تيم طاهر",
      "تييم طاهر",
      "فريق طاهر",
    ],
  },
  cairo: {
    label: "Cairo Team (Qaoud)",
    aliases: [
      "cairo",
      "cairo team",
      "team cairo",
      "qaoud",
      "kaoud",
      "qaaoud",
      "qaoud team",
      "القاهره",
      "القاهرة",
      "قاهره",
      "قاهرة",
      "قاعود",
      "تيم القاهره",
      "تيم القاهرة",
      "تييم القاهره",
      "تييم القاهرة",
      "تيم قاعود",
      "تييم قاعود",
      "فريق القاهره",
      "فريق القاهرة",
      "فريق قاعود",
    ],
  },
};

const REPORT_TYPES = {
  spend: {
    label: "Spend / Performance",
    aliases: [
      "spend",
      "performance",
      "daily",
      "daily report",
      "صرف",
      "الصرف",
      "انفاق",
      "الانفاق",
      "مصروف",
      "المصروف",
      "صرف يومي",
    ],
  },
  age: {
    label: "Age",
    aliases: [
      "age",
      "ages",
      "age breakdown",
      "سن",
      "السن",
      "اعمار",
      "الاعمار",
      "فئات عمريه",
      "الفئات العمريه",
    ],
  },
  governorate: {
    label: "Governorate",
    aliases: [
      "government",
      "governorate",
      "governorates",
      "region",
      "regions",
      "محافظه",
      "المحافظه",
      "محافظات",
      "المحافظات",
    ],
  },
  allocation: {
    label: "Allocation / Balance",
    aliases: [
      "allocation",
      "allocation report",
      "balance",
      "الالوكيشن",
      "الالوكشن",
      "اللوكيشن",
      "بالانس",
      "الرصيد",
    ],
  },
};

const FULL_REPORT_ALIASES = [
  "full",
  "full report",
  "all reports",
  "complete report",
  "كل التقارير",
  "تقرير شامل",
  "التقرير الشامل",
];

const REPORT_ORDER = [
  "spend",
  "age",
  "governorate",
  "allocation",
];


function translateDigits(value) {
  const map = {
    "٠": "0", "١": "1", "٢": "2", "٣": "3", "٤": "4",
    "٥": "5", "٦": "6", "٧": "7", "٨": "8", "٩": "9",
    "۰": "0", "۱": "1", "۲": "2", "۳": "3", "۴": "4",
    "۵": "5", "۶": "6", "۷": "7", "۸": "8", "۹": "9",
  };

  return String(value || "").replace(
    /[٠-٩۰-۹]/g,
    (char) => map[char] || char
  );
}


function normalizeText(value) {
  return translateDigits(value)
    .toLowerCase()
    .replace(/[\u064B-\u065F\u0670]/g, "")
    .replace(/\u0640/g, "")
    .replace(/[أإآ]/g, "ا")
    .replace(/ى/g, "ي")
    .replace(/[|,;:/\\()[\]{}]+/g, " ")
    .replace(/[-–—_]+/g, " ")
    .replace(/[!?؟.]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}


function normalizePhone(value) {
  let digits = String(value || "").replace(/\D/g, "");

  if (digits.startsWith("00")) {
    digits = digits.slice(2);
  }

  if (digits.length === 11 && digits.startsWith("01")) {
    digits = "20" + digits.slice(1);
  }

  return digits;
}


function padded(text) {
  return ` ${text} `;
}


function expandArabicClitics(text) {
  const tokens = normalizeText(text).split(/\s+/).filter(Boolean);
  const expanded = [];

  for (const token of tokens) {
    expanded.push(token);

    if (!/[\u0600-\u06FF]/.test(token)) {
      continue;
    }

    let current = token;

    // Egyptian/Arabic writing often attaches conjunctions/prepositions:
    // لعبدالله / للصرف / والمحافظات / بالسن
    for (let i = 0; i < 3; i += 1) {
      if (current.length <= 3) break;

      if (
        current.startsWith("و") ||
        current.startsWith("ف") ||
        current.startsWith("ب") ||
        current.startsWith("ك") ||
        current.startsWith("ل")
      ) {
        current = current.slice(1);
        expanded.push(current);
        continue;
      }

      break;
    }
  }

  return expanded.join(" ");
}


function phrasePresent(text, phrase) {
  const normalizedText = normalizeText(text);
  const expandedText = expandArabicClitics(normalizedText);
  const normalizedPhrase = normalizeText(phrase);

  if (!normalizedPhrase) return false;

  const target = ` ${normalizedPhrase} `;

  return (
    padded(normalizedText).includes(target) ||
    padded(expandedText).includes(target)
  );
}


function firstRange(text) {
  const normalized = normalizeText(text);

  const numericMatch = normalized.match(/^([1-6])(?:\s|$)/);
  if (numericMatch) {
    return {
      ...RANGE_BY_NUMBER[numericMatch[1]],
      matched: true,
    };
  }

  for (const range of RANGE_ALIASES) {
    for (const alias of range.aliases) {
      if (phrasePresent(normalized, alias)) {
        return {
          key: range.key,
          label: range.label,
          matched: true,
        };
      }
    }
  }

  return {
    key: "today",
    label: "Today",
    matched: false,
  };
}



function findTeams(text) {
  const normalized = normalizeText(text);
  const found = [];

  for (const [key, config] of Object.entries(TEAMS)) {
    const matched = config.aliases.some(
      (alias) => phrasePresent(normalized, alias)
    );

    if (matched) {
      found.push(key);
    }
  }

  return [...new Set(found)];
}

function findAgents(text) {
  const normalized = normalizeText(text);
  const found = [];

  for (const [code, config] of Object.entries(AGENTS)) {
    const matched = config.aliases.some(
      (alias) => phrasePresent(normalized, alias)
    );

    if (matched) {
      found.push(code);
    }
  }

  return [...new Set(found)];
}


function findReportTypes(text) {
  const normalized = normalizeText(text);

  const full = FULL_REPORT_ALIASES.some(
    (alias) => phrasePresent(normalized, alias)
  );

  if (full) {
    return {
      types: [...REPORT_ORDER],
      matched: true,
    };
  }

  const types = [];

  for (const reportType of REPORT_ORDER) {
    const config = REPORT_TYPES[reportType];

    const matched = config.aliases.some(
      (alias) => phrasePresent(normalized, alias)
    );

    if (matched) {
      types.push(reportType);
    }
  }

  return {
    types,
    matched: types.length > 0,
  };
}


function isExplicitAllAgents(text) {
  const normalized = normalizeText(text);

  return [
    "all agents",
    "all",
    "كل الناس",
    "كل الايجنتس",
    "كل الاجينتس",
    "كل الايجنت",
    "الكل",
  ].some(
    (alias) => phrasePresent(normalized, alias)
  );
}


function parseCommand(input) {
  const text = normalizeText(input);

  if (!text || MENU_TRIGGERS.has(text)) {
    return { type: "menu" };
  }

  const range = firstRange(text);
  const teams = findTeams(text);
  const agents = findAgents(text);
  const reportSelection = findReportTypes(text);
  const explicitAll = isExplicitAllAgents(text);

  if (teams.length > 1) {
    return {
      type: "invalid",
      reason: "multiple_teams",
    };
  }

  if (agents.length > 1) {
    return {
      type: "invalid",
      reason: "multiple_agents",
    };
  }

  // Default team is Taher unless Cairo/Qaoud is explicitly requested.
  const teamKey = (
    teams.length === 1
      ? teams[0]
      : "taher"
  );

  // Cairo/Qaoud is an OVERALL team and is not split by agent.
  const agentCode = (
    teamKey === "cairo"
      ? "ALL"
      : (
          agents.length === 1
            ? agents[0]
            : "ALL"
        )
  );

  let reportTypes = reportSelection.types;

  const recognizedAnything = (
    range.matched ||
    teams.length > 0 ||
    agents.length > 0 ||
    reportSelection.matched ||
    explicitAll
  );

  if (!recognizedAnything) {
    return {
      type: "invalid",
      reason: "no_meaning",
    };
  }

  // If the user only selects a date/team/agent,
  // the normal/default report is Spend.
  if (reportTypes.length === 0) {
    reportTypes = ["spend"];
  }

  return {
    type: "report",
    rangeKey: range.key,
    rangeLabel: range.label,
    teamKey,
    teamLabel: TEAMS[teamKey].label,
    agentCode,
    agentLabel: (
      teamKey === "cairo"
        ? "Overall — no agent split"
        : (
            agentCode === "ALL"
              ? "All Agents"
              : `${AGENTS[agentCode].name} (${agentCode})`
          )
    ),
    reportTypes,
    reportLabels: reportTypes.map(
      (type) => REPORT_TYPES[type].label
    ),
    rawCommand: String(input || ""),
  };
}

function menuText() {
  return [
    "📊 *SMART META ADS REPORTS*",
    "",
    "🏢 *Team*",
    "• Taher — Default لو مذكرتش Team",
    "• Cairo / Qaoud — تييم القاهرة / تييم قاعود",
    "",
    "📅 *Range*",
    "1. Today",
    "2. Yesterday",
    "3. Last 7 Days",
    "4. Last 30 Days",
    "5. This Month",
    "6. Last Month",
    "",
    "📑 *Report Type*",
    "• Spend — Spend / Leads / CPL + Male",
    "• Age — Spend / Leads / CPL by Age",
    "• Government — Spend / Leads / CPL by Governorate",
    "• Allocation — Budget / Balance / Coverage",
    "• Full — كل التقارير",
    "",
    "👤 *Taher Agents*",
    "AA — Abdallah Adel",
    "HM — Ahmed Hesham",
    "BM — Bassem Shalawy",
    "EK — Esraa Kamal",
    "MA — Mahmoud",
    "AF — Amr Fathy",
    "SQ — Ahmed Sharkawy",
    "OS — Osama Serwe",
    "MM — Mohamed Mahmoud",
    "NB — Mohamed Nabih",
    "",
    "ℹ️ *Cairo / Qaoud*",
    "التقرير Overall فقط ومش متقسم Agents.",
    "",
    "✅ *Examples*",
    "Today AA",
    "7day EK Age",
    "Today Cairo",
    "7day Qaoud Age",
    "3 Cairo Government",
    "Full Cairo",
    "",
    "🇪🇬 وتقدر تكتب طبيعي:",
    "عايز تقرير صرف انهاردة",
    "عايز تقرير لعبدالله انهاردة للصرف وال AGE",
    "عايز تقرير صرف انهاردة للقاهرة",
    "عايز السن لقاعود اخر 7 ايام",
    "عايز المحافظات لتييم القاهرة الشهر ده",
    "",
    "لو مذكرتش Team → Taher تلقائي.",
    "لو Cairo/Qaoud → Overall بدون Agents.",
    "لو من غير Report Type → Spend تلقائي.",
  ].join("\n");
}

function allowedNumbers(env) {
  const raw = String(env.ALLOWED_NUMBERS || "").trim();

  if (!raw) return new Set();

  return new Set(
    raw
      .split(/[,;\s]+/)
      .map(normalizePhone)
      .filter(Boolean)
  );
}


function isAllowedSender(sender, env) {
  const allowed = allowedNumbers(env);

  if (allowed.size === 0) {
    return false;
  }

  return allowed.has(normalizePhone(sender));
}


function reportSummary(command) {
  const lines = [
    "⏳ *جاري تجهيز التقرير...*",
    "",
    `🏢 ${command.teamLabel}`,
    `📅 ${command.rangeLabel}`,
  ];

  if (command.teamKey === "taher") {
    lines.push(`👤 ${command.agentLabel}`);
  } else {
    lines.push("📊 Overall — بدون تقسيم Agents");
  }

  lines.push(
    `📑 ${command.reportLabels.join(" + ")}`
  );

  if (
    command.reportTypes.includes("allocation") &&
    command.rangeKey !== "today"
  ) {
    lines.push(
      "",
      "ℹ️ Allocation بيستخدم الرصيد والـDaily Budget الحاليين."
    );
  }

  lines.push(
    "",
    "هيتبعتلك التقرير أول ما GitHub يخلص."
  );

  return lines.join("\n");
}

async function sendWhatsAppText(env, to, body) {
  const apiVersion = env.META_API_VERSION || "v26.0";

  const url =
    `https://graph.facebook.com/${apiVersion}/` +
    `${env.WHATSAPP_PHONE_NUMBER_ID}/messages`;

  const response = await fetch(url, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${env.WHATSAPP_ACCESS_TOKEN}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      messaging_product: "whatsapp",
      recipient_type: "individual",
      to: normalizePhone(to),
      type: "text",
      text: {
        preview_url: false,
        body,
      },
    }),
  });

  if (!response.ok) {
    const responseText = await response.text();

    throw new Error(
      `WhatsApp API ${response.status}: ` +
      responseText.slice(0, 1200)
    );
  }
}


async function dispatchGitHub(env, command, sender) {
  const workflow =
    env.GITHUB_WORKFLOW || "smart_report.yml";

  const ref =
    env.GITHUB_REF || "main";

  const url =
    `https://api.github.com/repos/` +
    `${env.GITHUB_OWNER}/${env.GITHUB_REPO}/` +
    `actions/workflows/${workflow}/dispatches`;

  const response = await fetch(url, {
    method: "POST",
    headers: {
      "Accept": "application/vnd.github+json",
      "Authorization": `Bearer ${env.GITHUB_TOKEN}`,
      "Content-Type": "application/json",
      "X-GitHub-Api-Version": "2026-03-10",
      "User-Agent": "smart-meta-whatsapp-report-bot",
    },
    body: JSON.stringify({
      ref,
      inputs: {
        range_key: command.rangeKey,
        team_key: command.teamKey,
        agent_code: command.agentCode,
        report_types: command.reportTypes.join(","),
        recipient: normalizePhone(sender),
        raw_command: command.rawCommand.slice(0, 500),
      },
    }),
  });

  if (!response.ok) {
    const responseText = await response.text();

    throw new Error(
      `GitHub dispatch ${response.status}: ` +
      responseText.slice(0, 1500)
    );
  }
}


function hexToBytes(hex) {
  const clean = String(hex).trim().toLowerCase();
  const output = new Uint8Array(clean.length / 2);

  for (let i = 0; i < clean.length; i += 2) {
    output[i / 2] = parseInt(clean.slice(i, i + 2), 16);
  }

  return output;
}


function constantTimeEqual(a, b) {
  if (a.length !== b.length) return false;

  let diff = 0;

  for (let i = 0; i < a.length; i += 1) {
    diff |= a[i] ^ b[i];
  }

  return diff === 0;
}


async function verifyMetaSignature(request, rawBody, env) {
  if (!env.META_APP_SECRET) {
    console.error("META_APP_SECRET is missing.");
    return false;
  }

  const header =
    request.headers.get("X-Hub-Signature-256") || "";

  if (!header.startsWith("sha256=")) {
    return false;
  }

  const suppliedHex =
    header.slice("sha256=".length);

  if (!/^[0-9a-fA-F]{64}$/.test(suppliedHex)) {
    return false;
  }

  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(env.META_APP_SECRET),
    {
      name: "HMAC",
      hash: "SHA-256",
    },
    false,
    ["sign"]
  );

  const expected = new Uint8Array(
    await crypto.subtle.sign(
      "HMAC",
      key,
      new TextEncoder().encode(rawBody)
    )
  );

  return constantTimeEqual(
    expected,
    hexToBytes(suppliedHex)
  );
}


function extractTextMessages(payload) {
  const messages = [];

  for (const entry of payload?.entry || []) {
    for (const change of entry?.changes || []) {
      const value = change?.value || {};

      for (const message of value?.messages || []) {
        if (message?.type !== "text") {
          continue;
        }

        const sender = message?.from;
        const body = message?.text?.body;

        if (
          sender &&
          typeof body === "string"
        ) {
          messages.push({
            sender,
            body,
          });
        }
      }
    }
  }

  return messages;
}


async function handleMessage(env, sender, body) {
  if (!isAllowedSender(sender, env)) {
    console.log(
      "Ignored unauthorized sender: " +
      normalizePhone(sender)
    );
    return;
  }

  const command = parseCommand(body);

  if (command.type === "menu") {
    await sendWhatsAppText(
      env,
      sender,
      menuText()
    );
    return;
  }

  if (command.type === "invalid") {
    await sendWhatsAppText(
      env,
      sender,
      [
        "❌ مش قادر أحدد التقرير المطلوب من الرسالة دي.",
        "",
        "ابعت *Menu* علشان تشوف كل الاختيارات والأمثلة.",
      ].join("\n")
    );
    return;
  }

  try {
    // Dispatch first. This prevents a false "processing" message
    // if GitHub rejects the request.
    await dispatchGitHub(
      env,
      command,
      sender
    );

    await sendWhatsAppText(
      env,
      sender,
      reportSummary(command)
    );
  } catch (error) {
    console.error(
      "SMART REPORT ERROR:",
      error?.message || error
    );

    await sendWhatsAppText(
      env,
      sender,
      [
        "❌ حصل خطأ في تشغيل التقرير.",
        "",
        "راجع Cloudflare Logs / GitHub Action.",
      ].join("\n")
    );
  }
}


export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    // Meta webhook verification.
    if (request.method === "GET") {
      const mode =
        url.searchParams.get("hub.mode");

      const token =
        url.searchParams.get("hub.verify_token");

      const challenge =
        url.searchParams.get("hub.challenge");

      if (
        mode === "subscribe" &&
        token &&
        token === env.WHATSAPP_VERIFY_TOKEN
      ) {
        return new Response(
          challenge || "",
          { status: 200 }
        );
      }

      return new Response(
        "Verification failed",
        { status: 403 }
      );
    }

    if (request.method !== "POST") {
      return new Response(
        "Method not allowed",
        { status: 405 }
      );
    }

    const rawBody =
      await request.text();

    const validSignature =
      await verifyMetaSignature(
        request,
        rawBody,
        env
      );

    if (!validSignature) {
      return new Response(
        "Invalid signature",
        { status: 401 }
      );
    }

    let payload;

    try {
      payload = JSON.parse(rawBody);
    } catch {
      return new Response(
        "Invalid JSON",
        { status: 400 }
      );
    }

    const messages =
      extractTextMessages(payload);

    for (const message of messages) {
      ctx.waitUntil(
        handleMessage(
          env,
          message.sender,
          message.body
        )
      );
    }

    return new Response(
      "EVENT_RECEIVED",
      { status: 200 }
    );
  },
};
