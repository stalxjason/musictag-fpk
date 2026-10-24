/**
 * 飞牛 NAS 原生音乐刮削服务 - Lyrico 插件统一宿主 (Lyrico Host Runner)
 * ====================================================================
 * 为 Lyrico-Plugins 规范的 JavaScript 插件提供 100% 兼容的 Platform 宿主环境。
 * 纯 Node.js 标准库实现，零第三方 npm 依赖。
 */

const os = require('os');
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const zlib = require('zlib');
const { spawnSync } = require('child_process');
const vm = require('vm');
const readline = require('readline');

// ==========================================
// 1. 同步 HTTP 请求实现 (基于系统 curl.exe)
// ==========================================
function httpRequestSync(method, url, body, options = {}) {
  // 校验协议，仅允许 http 与 https
  const parsedUrl = String(url || '').trim();
  if (!parsedUrl.startsWith('http://') && !parsedUrl.startsWith('https://')) {
    throw new Error('Disallowed URL scheme in plugin request: ' + parsedUrl);
  }

  // -s 静音，-L 跟随重定向，-g 禁用 glob 方括号解析，-i 包含响应头
  const args = ['-s', '-L', '-g', '-i', '-X', method];
  
  const headers = options.headers || {};
  if (options.contentType) {
    headers['Content-Type'] = options.contentType;
  }
  for (const [k, v] of Object.entries(headers)) {
    args.push('-H', `${k}: ${v}`);
  }
  
  if (options.readTimeoutMs) {
    const sec = Math.max(1, Math.ceil(options.readTimeoutMs / 1000));
    args.push('--max-time', String(sec));
  } else {
    args.push('--max-time', '15');
  }
  args.push('--connect-timeout', '10');

  let tempBodyFile = null;
  if (body) {
    // 写入系统的系统临时目录，而不是源码所在目录
    if (typeof body === 'string' && body.length > 512) {
      tempBodyFile = path.join(os.tmpdir(), `.lyrico_curl_tmp_${Date.now()}_${Math.random().toString(36).slice(2)}.tmp`);
      fs.writeFileSync(tempBodyFile, body, 'utf8');
      args.push('--data-binary', `@${tempBodyFile}`);
    } else {
      args.push('--data-binary', String(body));
    }
  }

  // 使用 '--' 阻断 URL 开头可能存在的参数注入
  args.push('--', parsedUrl);

  let res;
  try {
    const curlBin = process.platform === 'win32' ? 'curl.exe' : 'curl';
    res = spawnSync(curlBin, args, { encoding: 'buffer', maxBuffer: 8 * 1024 * 1024 });
  } finally {
    if (tempBodyFile && fs.existsSync(tempBodyFile)) {
      try { fs.unlinkSync(tempBodyFile); } catch (e) {}
    }
  }

  if (res.error) {
    throw res.error;
  }
  
  const raw = res.stdout || Buffer.alloc(0);
  
  // 精确解析 HTTP 响应头与 Body（跳过 100 Continue / 30x 重定向头）
  let offset = 0;
  let finalStatus = 200;
  let finalHeaders = {};
  let bodyBuffer = raw;

  while (offset < raw.length) {
    const crlf2 = raw.indexOf(Buffer.from('\r\n\r\n'), offset);
    const lf2 = raw.indexOf(Buffer.from('\n\n'), offset);
    let hEnd = -1;
    let sepLen = 4;
    if (crlf2 !== -1 && (lf2 === -1 || crlf2 <= lf2)) {
      hEnd = crlf2;
      sepLen = 4;
    } else if (lf2 !== -1) {
      hEnd = lf2;
      sepLen = 2;
    } else {
      break;
    }

    const hText = raw.subarray(offset, hEnd).toString('utf8');
    const firstLine = hText.split(/\r?\n/)[0];
    if (!firstLine.startsWith('HTTP/')) {
      break;
    }

    const statusCode = parseInt(firstLine.split(' ')[1] || '200', 10);
    offset = hEnd + sepLen;

    const lines = hText.split(/\r?\n/);
    const curHeaders = {};
    for (let i = 1; i < lines.length; i++) {
      const line = lines[i];
      const idx = line.indexOf(':');
      if (idx > 0) {
        const k = line.slice(0, idx).trim().toLowerCase();
        const v = line.slice(idx + 1).trim();
        if (k === 'set-cookie') {
          if (!curHeaders[k]) curHeaders[k] = [];
          curHeaders[k].push(v);
        } else {
          curHeaders[k] = v;
        }
      }
    }

    finalStatus = statusCode;
    finalHeaders = curHeaders;

    // 若为 1xx 或 3xx 重定向，检查后续是否紧接着下一个 HTTP 响应头
    if ((statusCode >= 100 && statusCode < 200) || (statusCode >= 300 && statusCode < 400)) {
      if (raw.subarray(offset, offset + 5).toString('utf8') === 'HTTP/') {
        continue;
      }
    }

    bodyBuffer = raw.subarray(offset);
    break;
  }

  return {
    code: finalStatus,
    headers: finalHeaders,
    body: bodyBuffer.toString('utf8'),
    bodyBase64: bodyBuffer.toString('base64')
  };
}

// ==========================================
// 2. 全局持久内存缓存
// ==========================================
const memoryCache = new Map();

// ==========================================
// 3. Platform 宿主环境标准实现
// ==========================================
const Platform = {
  app: {
    getUserAgent: () => "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Lyrico/0.5.0"
  },
  runtime: {
    getInfo: () => ({
      apiVersion: 5,
      supportedHostApis: [
        "xml.getRootAttributes",
        "xml.findElements",
        "xml.removeElements"
      ],
      host: "musictag-native-lyrico"
    })
  },
  crypto: {
    md5: (str) => crypto.createHash('md5').update(String(str)).digest('hex'),
    aesEcbPkcs5EncryptBase64: (text, key) => {
      const cipher = crypto.createCipheriv('aes-128-ecb', Buffer.from(key), null);
      return Buffer.concat([cipher.update(Buffer.from(text, 'utf8')), cipher.final()]).toString('base64');
    },
    aesEcbPkcs5DecryptBase64ToText: (b64, key) => {
      const decipher = crypto.createDecipheriv('aes-128-ecb', Buffer.from(key), null);
      return Buffer.concat([decipher.update(Buffer.from(b64, 'base64')), decipher.final()]).toString('utf8');
    },
    aesEcbPkcs5EncryptHex: (text, key) => {
      const cipher = crypto.createCipheriv('aes-128-ecb', Buffer.from(key), null);
      return Buffer.concat([cipher.update(Buffer.from(text, 'utf8')), cipher.final()]).toString('hex');
    }
  },
  base64: {
    decodeText: (b64) => Buffer.from(b64, 'base64').toString('utf8'),
    encodeText: (txt) => Buffer.from(txt, 'utf8').toString('base64'),
    decodeUrlText: (b64) => {
      let s = String(b64 || '').replace(/-/g, '+').replace(/_/g, '/');
      while (s.length % 4) s += '=';
      return Buffer.from(s, 'base64').toString('utf8');
    },
    dropBytes: (b64, n) => Buffer.from(b64, 'base64').subarray(n).toString('base64')
  },
  bytes: {
    xorBase64: (b64, keyBytes) => {
      const buf = Buffer.from(b64, 'base64');
      for (let i = 0; i < buf.length; i++) {
        buf[i] ^= keyBytes[i % keyBytes.length];
      }
      return buf.toString('base64');
    }
  },
  compression: {
    inflateBase64ToText: (b64) => zlib.inflateSync(Buffer.from(b64, 'base64')).toString('utf8'),
    inflateBytesToText: (bytes) => zlib.inflateSync(Buffer.from(bytes)).toString('utf8')
  },
  http: {
    getText: (url, opts) => {
      const resp = httpRequestSync('GET', url, null, opts);
      return resp.body;
    },
    postText: (url, body, opts) => {
      const resp = httpRequestSync('POST', url, body, opts);
      return resp.body;
    },
    get: (url, opts) => httpRequestSync('GET', url, null, opts),
    postBytesResponse: (url, body, opts) => httpRequestSync('POST', url, body, opts)
  },
  cache: {
    get: (k) => {
      const item = memoryCache.get(k);
      if (!item) return null;
      if (item.exp && Date.now() > item.exp) {
        memoryCache.delete(k);
        return null;
      }
      return item.val;
    },
    set: (k, v, ttlMs) => {
      const exp = ttlMs ? Date.now() + ttlMs : 0;
      memoryCache.set(k, { val: v, exp: exp });
    },
    remove: (k) => memoryCache.delete(k)
  },
  log: {
    debug: (tag, msg) => { /* console.error(`[DEBUG][${tag}]`, msg); */ },
    warn: (tag, msg) => console.error(`[WARN][${tag}]`, msg),
    error: (tag, msg) => console.error(`[ERROR][${tag}]`, msg)
  },
  i18n: {
    t: (key, ...args) => `${key} ${args.join(' ')}`
  },
  xml: {
    getRootAttributes: (xmlStr) => {
      const m = String(xmlStr).match(/<tt\s+([^>]+)>/i);
      if (!m) return {};
      const attrs = {};
      const attrRegex = /([a-zA-Z0-9_:\-]+)="([^"]*)"/g;
      let am;
      while ((am = attrRegex.exec(m[1])) !== null) {
        attrs[am[1]] = am[2];
      }
      return attrs;
    },
    findElements: (xmlStr, query) => {
      const tag = query.tag || 'translation';
      const regex = new RegExp(`<${tag}\\s+([^>]*)(?:>(.*?)<\\/${tag}>|\\/>)`, 'gis');
      const results = [];
      let m;
      while ((m = regex.exec(xmlStr)) !== null) {
        const attrStr = m[1];
        const content = m[2] || '';
        const attrs = {};
        const attrRegex = /([a-zA-Z0-9_:\-]+)="([^"]*)"/g;
        let am;
        while ((am = attrRegex.exec(attrStr)) !== null) {
          attrs[am[1]] = am[2];
        }
        let match = true;
        if (query.attrs) {
          for (const [k, v] of Object.entries(query.attrs)) {
            if (attrs[k] !== v) { match = false; break; }
          }
        }
        if (match) {
          results.push({ attrs, content, raw: m[0] });
        }
      }
      return results;
    },
    removeElements: (xmlStr, query) => {
      const tag = query.tag || 'translation';
      const regex = new RegExp(`<${tag}\\s+[^>]*>(?:.*?<\\/${tag}>|\\/>)`, 'gis');
      return xmlStr.replace(regex, '');
    },
    replaceChildrenByAttr: (xmlStr, query) => {
      return xmlStr;
    }
  }
};

// ==========================================
// 4. 插件沙箱创建与执行
// ==========================================
function executePluginAction(pluginDir, action, request) {
  const manifestPath = path.join(pluginDir, 'manifest.json');
  if (!fs.existsSync(manifestPath)) {
    throw new Error('manifest.json not found in ' + pluginDir);
  }
  const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));

  const safeConsole = {
    log: (...args) => process.stderr.write("[PluginLog] " + args.map(String).join(" ") + "\n"),
    info: (...args) => process.stderr.write("[PluginInfo] " + args.map(String).join(" ") + "\n"),
    warn: (...args) => process.stderr.write("[PluginWarn] " + args.map(String).join(" ") + "\n"),
    error: (...args) => process.stderr.write("[PluginError] " + args.map(String).join(" ") + "\n"),
    dir: (...args) => process.stderr.write("[PluginDir] " + args.map(a => JSON.stringify(a)).join(" ") + "\n")
  };

  const sandbox = {
    Platform: Platform,
    console: safeConsole,
    Date: Date,
    Math: Math,
    JSON: JSON,
    parseInt: parseInt,
    parseFloat: parseFloat,
    isFinite: isFinite,
    isNaN: isNaN,
    encodeURIComponent: encodeURIComponent,
    decodeURIComponent: decodeURIComponent,
    Array: Array,
    Object: Object,
    String: String,
    Number: Number,
    Boolean: Boolean,
    RegExp: RegExp,
    Error: Error
  };
  sandbox.global = sandbox;
  sandbox.window = sandbox;

  const context = vm.createContext(sandbox);

  // 加载 includeDirs 中的库脚本
  const includeDirs = manifest.includeDirs || [];
  for (const dirName of includeDirs) {
    const fullDir = path.join(pluginDir, dirName);
    if (fs.existsSync(fullDir)) {
      const files = fs.readdirSync(fullDir).filter(f => f.endsWith('.js')).sort();
      for (const f of files) {
        const code = fs.readFileSync(path.join(fullDir, f), 'utf8');
        vm.runInContext(code, context, { filename: f, timeout: 15000 });
      }
    }
  }

  // 加载入口脚本 (通常是 source.js)
  const entryFile = manifest.entry || 'source.js';
  const entryCode = fs.readFileSync(path.join(pluginDir, entryFile), 'utf8');
  vm.runInContext(entryCode, context, { filename: entryFile, timeout: 15000 });

  if (typeof context[action] !== 'function') {
    throw new Error(`Action function '${action}' not found in plugin ${manifest.id}`);
  }

  context.__targetAction = context[action];
  context.__targetRequest = request || {};
  return vm.runInContext("__targetAction(__targetRequest)", context, { timeout: 20000 });
}

// ==========================================
// 5. 交互协议 (支持命令行单次执行与常驻 JSON-RPC)
// ==========================================
if (process.argv[2] === '--server') {
  // 常驻 JSON-RPC 模式 (通过 stdin/stdout 单行交互)
  const rl = readline.createInterface({ input: process.stdin, output: process.stdout, terminal: false });
  rl.on('line', (line) => {
    line = line.trim();
    if (!line) return;
    try {
      const task = JSON.parse(line);
      const res = executePluginAction(task.pluginDir, task.action, task.request);
      process.stdout.write(JSON.stringify({ ok: true, data: res }) + '\n');
    } catch (e) {
      process.stdout.write(JSON.stringify({ ok: false, error: e.message || String(e) }) + '\n');
    }
  });
} else if (process.argv.length >= 4) {
  // 单次命令行执行模式: node lyrico_host.js <pluginDir> <action> [requestJson]
  const pluginDir = process.argv[2];
  const action = process.argv[3];
  let request = {};
  if (process.argv[4]) {
    try {
      request = JSON.parse(process.argv[4]);
    } catch (e) {
      request = { keyword: process.argv[4] };
    }
  }
  try {
    const result = executePluginAction(pluginDir, action, request);
    console.log(JSON.stringify(result));
  } catch (e) {
    console.error("Execution error:", e);
    process.exit(1);
  }
} else {
  console.log("Usage: node lyrico_host.js <pluginDir> <action> [requestJson]");
  console.log("   or: node lyrico_host.js --server");
}
