// 联系方式与「复制邮箱」——整站只有这一份联系信息，组件不允许硬编码邮箱。

export type Contact = {
  /** 页面上展示的署名（改这里就改整站）。 */
  displayName: string;
  email: string;
  /** 可选：源码仓库主页。留空则不渲染该入口。 */
  github?: string;
};

export const contact: Contact = {
  displayName: "haixing",
  email: "haixing1104@gmail.com",
};

/**
 * 组装 `mailto:` 链接。
 *
 * 细节：空格用 `encodeURIComponent` 编成 `%20` 而不是 `+` —— 部分邮件客户端
 * （含 iOS 自带）会把 `+` 当字面量，主题里出现「a+b」这类肉眼可见的脏字符。
 */
export function mailtoUrl(email: string, subject?: string, body?: string): string {
  const params: string[] = [];
  if (subject) params.push(`subject=${encodeURIComponent(subject)}`);
  if (body) params.push(`body=${encodeURIComponent(body)}`);
  return `mailto:${email}${params.length > 0 ? `?${params.join("&")}` : ""}`;
}

/**
 * 复制文本到剪贴板。
 *
 * 两层策略（不是可选项）：Clipboard API 需要**安全上下文**（https 或 localhost），
 * 而宣传页很可能先被丢到一个没有证书的临时地址（`http://<IP>:5175`）上给人看；
 * 此时 `navigator.clipboard` 是 undefined，必须退回 `textarea + execCommand`。
 * 两条路都失败才返回 false（调用方据此提示「手动复制」）。
 */
export async function copyText(text: string): Promise<boolean> {
  const clipboard = typeof navigator !== "undefined" ? navigator.clipboard : undefined;
  if (clipboard?.writeText) {
    try {
      await clipboard.writeText(text);
      return true;
    } catch {
      // 权限被拒 / 非安全上下文：落到下面的兜底
    }
  }
  if (typeof document === "undefined") return false;
  try {
    const textarea = document.createElement("textarea");
    textarea.value = text;
    textarea.setAttribute("readonly", "");
    // 固定在视口外且透明：复制时页面不跳动、不闪
    textarea.style.position = "fixed";
    textarea.style.top = "-1000px";
    textarea.style.opacity = "0";
    document.body.appendChild(textarea);
    textarea.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(textarea);
    return ok;
  } catch {
    return false;
  }
}
