// base64url → 字符串（纯 JS，**不依赖 atob / Buffer**）。
//
// 为什么要自己写: RN（Hermes）既没有 `atob`（浏览器的），也没有 Node 的 `Buffer`。
// 而共享层 `http.ts` 要靠解 JWT 的 `exp` 做「到期前 ~30s 主动续签」—— 这是平台端口
// `jwtExpMs` 的唯一实现差异点；写错的表现是**静默失效**（只剩 401 兜底，用户会觉得"用着用着被踢"）。
//
// 兼容性: JWT 的 base64url 段**不带填充**、且用 `-`/`_` 代替 `+`/`/`，这里一并归一化。
const ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

/** base64url/base64 → 字节数组；含非法字符时返回 null（调用方兜底，不抛错）。 */
function decodeToBytes(input: string): Uint8Array | null {
  const normalized = input.replace(/-/g, "+").replace(/_/g, "/").replace(/=+$/, "");
  const bytes: number[] = [];
  let buffer = 0;
  let bits = 0;
  for (let i = 0; i < normalized.length; i += 1) {
    const value = ALPHABET.indexOf(normalized[i]);
    if (value < 0) return null;
    buffer = (buffer << 6) | value;
    bits += 6;
    if (bits >= 8) {
      bits -= 8;
      bytes.push((buffer >> bits) & 0xff);
    }
  }
  return Uint8Array.from(bytes);
}

/**
 * 手写 UTF-8 解码（JWT 载荷里可能带中文用户名等非 ASCII 内容）。
 * 非法序列按 U+FFFD 处理，绝不抛错 —— 解析失败由调用方退化为"不排续签定时器"。
 */
export function utf8Decode(bytes: Uint8Array): string {
  let out = "";
  for (let i = 0; i < bytes.length; ) {
    const byte = bytes[i];
    if (byte < 0x80) {
      out += String.fromCharCode(byte);
      i += 1;
      continue;
    }
    let extra = 0;
    let code = 0;
    if ((byte & 0xe0) === 0xc0) {
      extra = 1;
      code = byte & 0x1f;
    } else if ((byte & 0xf0) === 0xe0) {
      extra = 2;
      code = byte & 0x0f;
    } else if ((byte & 0xf8) === 0xf0) {
      extra = 3;
      code = byte & 0x07;
    } else {
      out += "\uFFFD";
      i += 1;
      continue;
    }
    if (i + extra >= bytes.length) {
      out += "\uFFFD";
      break;
    }
    let valid = true;
    for (let k = 1; k <= extra; k += 1) {
      const next = bytes[i + k];
      if ((next & 0xc0) !== 0x80) {
        valid = false;
        break;
      }
      code = (code << 6) | (next & 0x3f);
    }
    if (!valid) {
      out += "\uFFFD";
      i += 1;
      continue;
    }
    out += String.fromCodePoint(code);
    i += extra + 1;
  }
  return out;
}

/**
 * base64url（可带/可省填充）→ 字符串；非法输入返回 null。
 *
 * 优先用运行时自带的 `TextDecoder`（expo 的 winter runtime 会全局安装它），
 * 拿不到再退到手写实现 —— 两条路径都保证「解 exp」不会静默失效。
 */
export function decodeBase64UrlToString(input: string): string | null {
  const bytes = decodeToBytes(input);
  if (bytes === null) return null;
  const Decoder = (globalThis as { TextDecoder?: new (encoding?: string) => { decode(b: Uint8Array): string } })
    .TextDecoder;
  if (Decoder) {
    try {
      return new Decoder("utf-8").decode(bytes);
    } catch {
      // 落到手写实现
    }
  }
  return utf8Decode(bytes);
}
