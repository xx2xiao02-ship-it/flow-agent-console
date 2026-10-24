// Wire keys are part of the existing CLI/API contract. Keep them unchanged.
// Mapping evidence (observed September 2026 Flow frontend):
// https://github.com/crisng95/flowkit/blob/main/docs/IMAGE_API.md
// NARWHAL is Nano Banana 2, not the newer Nano Banana 2.1.
// BELUGA confirmed in this account's live HTrJv catalog on 2026-10-19;
// https://github.com/TheSmallHanCat/flow2api/issues/187
export const imageModels = [
  { value: 'beluga', label: 'Nano Banana 2.1（新版 · Beluga）' },
  { value: 'narwhal', label: 'Nano Banana 2（标准版 · Narwhal）' },
  { value: 'harbor_seal', label: 'Nano Banana 2 Lite（轻量版 · Harbor Seal）' },
  { value: 'gem_pix_2', label: 'Nano Banana Pro（专业版 · Gem Pix 2）' },
] as const

export const imageModelNote = '新版账号优先选择 Nano Banana 2.1；Nano Banana 2 仅用于旧版账号。提交前检查所选账号的实时模型目录，不支持时停止提交，不自动换模型。'
export const imageModelProtocol = imageModels.map(({ value, label }) => `${value} → ${label}`).join('；')
