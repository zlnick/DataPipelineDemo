/**
 * 站内跳转路径的「语言前缀」处理。
 *
 * 本项目中英界面是**两套并列路由**（`/` 与 `/en/` 各一份，见 router/index.js），
 * 因此任何站内跳转都必须带语言前缀 —— 否则从英文界面跳过去会落到中文路由，
 * 表现为「点一下按钮就变成中文界面」（实测缺陷 2026-09-29）。
 *
 * 用法：`router.push(localePath('/mappings'))`、`:to="localePath('/assets')"`
 */
export function localePath(path) {
  const p = path.startsWith('/') ? path : `/${path}`
  const current = window.location.pathname
  const isEn = current === '/en' || current.startsWith('/en/')
  if (!isEn) return p
  if (p === '/') return '/en'
  return p.startsWith('/en/') ? p : `/en${p}`
}
